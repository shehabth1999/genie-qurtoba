"""Build / update the workflow-v2 graph («Qurtoba Accountant Automations») from a Python spec.

    manage.py qurtoba_workflow_v2                 # upsert nodes + edges of workflow 3 (idempotent)
    manage.py qurtoba_workflow_v2 --dry-run       # print the spec, change nothing
    manage.py qurtoba_workflow_v2 --canary 4      # route ONE partner to workflow 3
    manage.py qurtoba_workflow_v2 --release       # point WhatsApp account 3 at workflow 3
    manage.py qurtoba_workflow_v2 --rollback      # account back to workflow 2, drop partner overrides

The graph is versioned here (git), not on the canvas: re-running the command restores
it exactly. Nodes copied from workflow 2 (the shared-core / service-availability function
nodes and the payments agent) are read from the live workflow 2 at build time so the
prompt text never drifts between the two.
"""
import json
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from qurtoba.automation import replies as R
from qurtoba.automation.nodes import NODE_CODE

TARGET_WORKFLOW_ID = 3
SOURCE_WORKFLOW_ID = 2
WHATSAPP_ACCOUNT_ID = 3

SHARED_CORE_NODE = 'function_1783509447802'
AVAILABILITY_NODE = 'function_1780949181829'
SRC_PAYMENTS_NODE = 'agent_chat_1783507166437'
SRC_CASH_NODE = 'agent_chat_1783507168037'
SRC_NOT_LINKED_TOOL = 'tool_1781113475079'

FREETEXT_TOOLS = (
    'qurtoba_send_customer_balance_to_chat', 'qurtoba_get_customer_daily_transactions',
    'qurtoba_check_transaction_status', 'qurtoba_check_payment_status', 'alert_qurtoba_human',
    'whatsapp_reply_to_message', 'qurtoba_send_static_message',
)

INTENT_BRANCHES = ['transfer', 'balance', 'statement', 'status', 'cancel', 'social', 'off_hours', 'receipt', 'noise']
INTENT_TARGET = {
    'transfer': 'function_transfers', 'balance': 'function_balance', 'statement': 'function_statement',
    'status': 'function_status', 'cancel': 'function_cancel', 'social': 'function_social',
    'off_hours': 'function_off_hours', 'receipt': AVAILABILITY_NODE, 'noise': 'function_noise',
}

_PROMPT_PATH = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, 'prompts', 'agents', 'freetext', 'prompt.md')


def _freetext_prompt() -> str:
    with open(_PROMPT_PATH, encoding='utf-8') as fh:
        text = fh.read()
    return text.split('**prompt:**', 1)[1].strip() if '**prompt:**' in text else text.strip()


def _fn(node_id, label, x, y, code, timeout=60):
    return dict(node_id=node_id, node_type='function', label=label, x=x, y=y,
                configuration={'code': code, 'description': '', 'update_state': [], 'timeout_seconds': timeout})


def build_spec(src_nodes, tool_ids):
    """(nodes, edges, global_configuration) for the v2 graph."""
    def src_cfg(node_id):
        n = src_nodes.get(node_id)
        if n is None:
            raise CommandError(f'workflow {SOURCE_WORKFLOW_ID} has no node {node_id}')
        return json.loads(json.dumps(n.configuration))

    intent_conditions = [
        {'operator': 'equals', 'data_type': 'string', 'variable1': '{{ function_route.intent }}', 'variable2': name}
        for name in INTENT_BRANCHES
    ]
    payments = src_cfg(SRC_PAYMENTS_NODE)
    payments['handoff'] = {'enabled': False, 'targets': []}
    payments['update_state'] = []

    freetext = src_cfg(SRC_CASH_NODE)
    freetext['messages'] = [{'role': 'system', 'text': _freetext_prompt(), 'cache': True, 'cache_ttl': '5m', 'attachments': []}]
    freetext['selected_tools'] = [{'store': True, 'tool_id': tool_ids[name], 'ask_human': False} for name in FREETEXT_TOOLS]
    freetext['handoff'] = {'enabled': True, 'targets': [{
        'node_id': 'agent_payments', 'tool_name': '',
        'tool_description': 'Register سداد payments from a receipt image (شراء كاش / شراء فورى), or explicit payment wording («العميل دفع»).',
    }]}
    freetext['update_state'] = []
    freetext['max_iterations'] = 4
    freetext['max_tokens'] = 1500
    freetext['temperature'] = 0.2
    freetext['reasoning_mode'] = 'none'
    freetext['description'] = 'Small model for free-text turns only — no money tools.'

    not_linked = src_cfg(SRC_NOT_LINKED_TOOL)
    not_linked['arguments'] = {'message': R.NOT_LINKED}

    X0, X1, X2, X3, X4, X5 = 0, 320, 640, 980, 1320, 1660
    nodes = [
        dict(node_id='conditional_linked', node_type='conditional', label='linked to a Qurtoba customer?', x=X0, y=400,
             configuration={'conditions': [{'operator': 'is_true', 'data_type': 'boolean',
                                            'variable1': '{{partner.has_qurtoba_customer}}', 'variable2': ''}],
                            'default_branch': 'default'}),
        dict(node_id='tool_not_linked', node_type='tool', label='not linked → static notice', x=X1, y=760, configuration=not_linked),
        _fn('function_route', 'ROUTER (deterministic)', X1, 400, NODE_CODE['function_route']),
        dict(node_id='conditional_intent', node_type='conditional', label='intent?', x=X2, y=400,
             configuration={'conditions': intent_conditions, 'default_branch': 'default'}),
        _fn('function_transfers', '1 transfer → planner + create (no model)', X3, 0, NODE_CODE['function_transfers'], timeout=120),
        _fn('function_balance', '2 balance', X3, 130, NODE_CODE['function_balance']),
        _fn('function_statement', '3 statement', X3, 260, NODE_CODE['function_statement'], timeout=120),
        _fn('function_status', '4 status', X3, 390, NODE_CODE['function_status']),
        _fn('function_cancel', '5 cancel', X3, 520, NODE_CODE['function_cancel']),
        _fn('function_social', '6 courtesy', X3, 650, NODE_CODE['function_social']),
        _fn('function_off_hours', '7 off-hours', X3, 780, NODE_CODE['function_off_hours']),
        _fn('function_noise', '9 noise (nothing to do)', X3, 1040, NODE_CODE['function_noise']),
        dict(node_id=AVAILABILITY_NODE, node_type='function', label='service_availability', x=X3, y=910,
             configuration=src_cfg(AVAILABILITY_NODE)),
        dict(node_id=SHARED_CORE_NODE, node_type='function', label='shared_roles', x=X4, y=910,
             configuration=src_cfg(SHARED_CORE_NODE)),
        dict(node_id='agent_payments', node_type='agent_chat', label='8 payments_agent (vision model)', x=X5, y=910, configuration=payments),
        _fn('function_freetext_context', 'else: context for the small model', X3, 1170, NODE_CODE['function_freetext_context']),
        dict(node_id='agent_freetext', node_type='agent_chat', label='freetext_agent (small model, no money tools)', x=X4, y=1170, configuration=freetext),
    ]
    edges = [
        ('conditional_linked', 'function_route', '1'),
        ('conditional_linked', 'tool_not_linked', '0'),
        ('function_route', 'conditional_intent', ''),
    ]
    for i, name in enumerate(INTENT_BRANCHES, 1):
        edges.append(('conditional_intent', INTENT_TARGET[name], str(i)))
    edges += [
        ('conditional_intent', 'function_freetext_context', '0'),
        (AVAILABILITY_NODE, SHARED_CORE_NODE, ''),
        (SHARED_CORE_NODE, 'agent_payments', ''),
        ('function_freetext_context', 'agent_freetext', ''),
    ]
    global_configuration = {
        'schedules': [], 'chat_based': False, 'input_message': '', 'record_trigger': {}, 'recursion_limit': 25,
        'state_injections': [{'key': 'off_hours', 'type': 'bool', 'persist': False, 'initial_value': False}],
        'schedules_runtime': [],
    }
    return nodes, edges, global_configuration


class Command(BaseCommand):
    help = 'Create/update the workflow-v2 graph (automation first, small model for free text).'

    def add_arguments(self, parser):
        parser.add_argument('--workflow-id', type=int, default=TARGET_WORKFLOW_ID)
        parser.add_argument('--source-workflow', type=int, default=SOURCE_WORKFLOW_ID)
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--canary', type=int, metavar='PARTNER_ID', help='route this partner to the v2 workflow')
        parser.add_argument('--release', action='store_true', help=f'point WhatsApp account {WHATSAPP_ACCOUNT_ID} at the v2 workflow')
        parser.add_argument('--rollback', action='store_true', help='account back to the source workflow; drop partner overrides')

    def handle(self, *args, **opts):
        from modules.aistudio.models import ToolDefinition, WorkflowDefinition, WorkflowEdge, WorkflowNode
        wf_id, src_id = opts['workflow_id'], opts['source_workflow']

        if opts['canary'] or opts['release'] or opts['rollback']:
            return self._rollout(opts, wf_id, src_id)

        wf = WorkflowDefinition.objects.filter(pk=wf_id).first()
        src = WorkflowDefinition.objects.filter(pk=src_id).first()
        if wf is None or src is None:
            raise CommandError(f'workflow {wf_id} or {src_id} not found')
        src_nodes = {n.node_id: n for n in WorkflowNode.objects.filter(workflow=src)}
        tool_ids = dict(ToolDefinition.objects.filter(name__in=FREETEXT_TOOLS).values_list('name', 'id'))
        missing = [t for t in FREETEXT_TOOLS if t not in tool_ids]
        if missing:
            raise CommandError(f'tools not registered in ToolDefinition: {missing}')

        nodes, edges, gconf = build_spec(src_nodes, tool_ids)
        if opts['dry_run']:
            for n in nodes:
                self.stdout.write(f"NODE {n['node_id']:32s} {n['node_type']:12s} {n['label']}")
            for s, t, h in edges:
                self.stdout.write(f"EDGE {s} -[{h or '·'}]-> {t}")
            return

        with transaction.atomic():
            wf.workflow_type = 'partner_flow'
            wf.global_configuration = gconf
            wf.error_message = src.error_message or wf.error_message
            wf.is_active = True
            wf.save()
            keep = set()
            for n in nodes:
                keep.add(n['node_id'])
                WorkflowNode.objects.update_or_create(
                    workflow=wf, node_id=n['node_id'],
                    defaults={'node_type': n['node_type'], 'label': n['label'], 'x_position': n['x'],
                              'y_position': n['y'], 'width': 220, 'height': 100, 'color': '',
                              'configuration': n['configuration']},
                )
            removed = WorkflowNode.objects.filter(workflow=wf).exclude(node_id__in=keep)
            n_removed = removed.count()
            removed.delete()
            WorkflowEdge.objects.filter(workflow=wf).delete()
            for s, t, h in edges:
                WorkflowEdge.objects.create(workflow=wf, source_node_id=s, target_node_id=t,
                                            source_handle=h, target_handle='', priority=1, label='', style={})
        self.stdout.write(f'workflow {wf_id} «{wf.name}»: {len(nodes)} nodes, {len(edges)} edges upserted, '
                          f'{n_removed} stale nodes removed')

    def _rollout(self, opts, wf_id, src_id):
        from modules.base.models import Partner
        from modules.whatsapp.models import WhatsAppAccount
        if opts['canary']:
            n = Partner.all_objects.filter(pk=opts['canary']).update(workflow_id=wf_id)
            self.stdout.write(f'partner {opts["canary"]}: workflow override → {wf_id} ({n} row)')
        if opts['release']:
            WhatsAppAccount.objects.filter(pk=WHATSAPP_ACCOUNT_ID).update(workflow_id=wf_id)
            self.stdout.write(f'WhatsApp account {WHATSAPP_ACCOUNT_ID}: workflow → {wf_id}')
        if opts['rollback']:
            WhatsAppAccount.objects.filter(pk=WHATSAPP_ACCOUNT_ID).update(workflow_id=src_id)
            n = Partner.all_objects.filter(workflow_id=wf_id).update(workflow_id=None)
            self.stdout.write(f'WhatsApp account {WHATSAPP_ACCOUNT_ID}: workflow → {src_id}; {n} partner override(s) cleared')
