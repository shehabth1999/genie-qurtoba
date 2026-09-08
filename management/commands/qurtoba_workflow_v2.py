"""Build / update the workflow-v2 graph («Qurtoba Accountant Automations») from a Python spec.

Graph: linked? → route → [receipt → payments agent | off-hours → notice | MONEY PATH (creates, no model)
       → anything left? → thinking model (replies + info tools) | done].

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

THINKER_TOOLS = (
    'whatsapp_reply_to_message', 'qurtoba_send_customer_balance_to_chat', 'qurtoba_get_customer_daily_transactions',
    'qurtoba_check_transaction_status', 'qurtoba_check_payment_status', 'qurtoba_clear_pending_transfers',
    'alert_qurtoba_human', 'qurtoba_send_static_message',
    'qurtoba_create_new_transactions_bulk', 'qurtoba_answer_pending',
)


def _ensure_tool_definitions(names):
    """A ToolDefinition row per registered @tool the thinker uses (the agent node selects by id)."""
    from modules.aistudio.models import ToolDefinition
    from modules.aistudio.tools.decorators import ToolRegistry
    import qurtoba.tools  # noqa: F401  (registers the extension's tools)
    created = []
    for name in names:
        if ToolDefinition.objects.filter(name=name).exists():
            continue
        info = ToolRegistry.get_tool(name)
        if info is None:
            continue
        ToolDefinition.objects.create(
            name=info.name, display_name=info.display_name or info.name, description=info.description or '',
            category=info.category or 'qurtoba', module_path=info.module_path, function_name=info.function_name,
            parameters_schema=info.parameters_schema or {}, return_schema=info.return_schema or {},
            requires_auth=bool(info.requires_auth), is_active=True,
        )
        created.append(name)
    return created

_PROMPT_PATH = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, 'prompts', 'agents', 'thinker', 'prompt.md')


def _thinker_prompt() -> str:
    with open(_PROMPT_PATH, encoding='utf-8') as fh:
        text = fh.read()
    return text.split('**prompt:**', 1)[1].strip() if '**prompt:**' in text else text.strip()


def _fn(node_id, label, x, y, code, timeout=60):
    return dict(node_id=node_id, node_type='function', label=label, x=x, y=y,
                configuration={'code': code, 'description': '', 'update_state': [], 'timeout_seconds': timeout})


def build_spec(src_nodes, tool_ids):
    """(nodes, edges, global_configuration) for the v2 graph:
    linked? → route → [receipt → payments agent | off-hours → notice | money path → needs AI? → thinker | done]"""
    def src_cfg(node_id):
        n = src_nodes.get(node_id)
        if n is None:
            raise CommandError(f'workflow {SOURCE_WORKFLOW_ID} has no node {node_id}')
        return json.loads(json.dumps(n.configuration))

    payments = src_cfg(SRC_PAYMENTS_NODE)
    payments['handoff'] = {'enabled': False, 'targets': []}
    payments['update_state'] = []

    thinker = src_cfg(SRC_CASH_NODE)
    thinker['messages'] = [{'role': 'system', 'text': _thinker_prompt(), 'cache': True, 'cache_ttl': '5m', 'attachments': []}]
    thinker['selected_tools'] = [{'store': True, 'tool_id': tool_ids[name], 'ask_human': False} for name in THINKER_TOOLS]
    thinker['handoff'] = {'enabled': True, 'targets': [{
        'node_id': 'agent_payments', 'tool_name': '',
        'tool_description': 'Register سداد payments from a receipt image (شراء كاش / شراء فورى), or explicit payment wording («العميل دفع»).',
    }]}
    thinker['update_state'] = []
    # owner decision 2026-09-06: DeepSeek V4 Flash (31) reads the customer better than Haiku on
    # this task (Haiku asked where it should act); Haiku 4.5 (21) stays the backup for outages
    thinker['llm_model_id'] = 31
    thinker['backup_llm_model_id'] = 21
    thinker['max_iterations'] = 6
    thinker['max_tokens'] = 1500
    thinker['temperature'] = 0.2
    thinker['reasoning_mode'] = 'none'
    thinker['description'] = 'Thinking model: runs after the system created the clean transfers; creates only what the system could not read.'

    not_linked = src_cfg(SRC_NOT_LINKED_TOOL)
    not_linked['arguments'] = {'message': R.NOT_LINKED}

    X0, X1, X2, X3, X4, X5, X6, X7 = 0, 320, 640, 960, 1280, 1600, 1920, 2240
    nodes = [
        dict(node_id='conditional_linked', node_type='conditional', label='linked to a Qurtoba customer?', x=X0, y=300,
             configuration={'conditions': [{'operator': 'is_true', 'data_type': 'boolean',
                                            'variable1': '{{partner.has_qurtoba_customer}}', 'variable2': ''}],
                            'default_branch': 'default'}),
        dict(node_id='tool_not_linked', node_type='tool', label='not linked → static notice', x=X1, y=560, configuration=not_linked),
        _fn('function_route', 'ROUTE: receipt image? off-hours? else money', X1, 300, NODE_CODE['function_route']),
        dict(node_id='conditional_route', node_type='conditional', label='receipt / off-hours / money', x=X2, y=300,
             configuration={'conditions': [
                 {'operator': 'equals', 'data_type': 'string', 'variable1': '{{ function_route.intent }}', 'variable2': 'receipt'},
                 {'operator': 'equals', 'data_type': 'string', 'variable1': '{{ function_route.intent }}', 'variable2': 'off_hours'},
             ], 'default_branch': 'default'}),
        _fn('function_transfers', 'MONEY PATH: planner → create (no model)', X3, 300, NODE_CODE['function_transfers'], timeout=120),
        dict(node_id='conditional_needs_ai', node_type='conditional', label='anything left for the AI?', x=X4, y=300,
             configuration={'conditions': [{'operator': 'is_true', 'data_type': 'boolean',
                                            'variable1': '{{ function_transfers.needs_ai }}', 'variable2': ''}],
                            'default_branch': 'default'}),
        _fn('function_done', 'done — silent turn', X5, 440, NODE_CODE['function_done']),
        _fn('function_ai_context', 'context for the thinking model', X5, 300, NODE_CODE['function_ai_context']),
        dict(node_id='agent_thinker', node_type='agent_chat', label='THINKER (model): questions, replies, info tools', x=X6, y=300, configuration=thinker),
        _fn('function_model_done', 'model turn timing → output', X7, 300, NODE_CODE['function_model_done']),
        _fn('function_off_hours', 'off-hours notice', X3, 560, NODE_CODE['function_off_hours']),
        dict(node_id=AVAILABILITY_NODE, node_type='function', label='service_availability', x=X3, y=40,
             configuration=src_cfg(AVAILABILITY_NODE)),
        dict(node_id=SHARED_CORE_NODE, node_type='function', label='shared_roles', x=X4, y=40,
             configuration=src_cfg(SHARED_CORE_NODE)),
        dict(node_id='agent_payments', node_type='agent_chat', label='payments_agent (vision model)', x=X5, y=40, configuration=payments),
    ]
    edges = [
        ('conditional_linked', 'function_route', '1'),
        ('conditional_linked', 'tool_not_linked', '0'),
        ('function_route', 'conditional_route', ''),
        ('conditional_route', AVAILABILITY_NODE, '1'),
        ('conditional_route', 'function_off_hours', '2'),
        ('conditional_route', 'function_transfers', '0'),
        ('function_transfers', 'conditional_needs_ai', ''),
        ('conditional_needs_ai', 'function_ai_context', '1'),
        ('conditional_needs_ai', 'function_done', '0'),
        ('function_ai_context', 'agent_thinker', ''),
        ('agent_thinker', 'function_model_done', ''),
        (AVAILABILITY_NODE, SHARED_CORE_NODE, ''),
        (SHARED_CORE_NODE, 'agent_payments', ''),
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
        new_defs = _ensure_tool_definitions(THINKER_TOOLS)
        if new_defs:
            self.stdout.write(f'tool definitions created: {new_defs}')
        tool_ids = dict(ToolDefinition.objects.filter(name__in=THINKER_TOOLS).values_list('name', 'id'))
        missing = [t for t in THINKER_TOOLS if t not in tool_ids]
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
