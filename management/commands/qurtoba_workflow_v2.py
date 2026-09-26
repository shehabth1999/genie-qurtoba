"""Build / update the workflow-v2 graph («Qurtoba Accountant Automations») from a Python spec.

Graph: AI switch on? [off → nothing at all] → linked? → route → [off-hours switch → closed agent (balance + statement only)
       | receipt → payments agent | MONEY PATH (creates, no model) → anything left? → thinking model | done].

Both switches live on the WhatsApp account («إعدادات قرطبة»), are flipped by hand and read by
qurtoba.switches. Nothing in this graph looks at the clock (owner decision 2026-09-13).

    manage.py qurtoba_workflow_v2                 # upsert nodes + edges of workflow 3 (idempotent)
    manage.py qurtoba_workflow_v2 --dry-run       # print the spec, change nothing
    manage.py qurtoba_workflow_v2 --canary 4      # route ONE partner to workflow 3
    manage.py qurtoba_workflow_v2 --release       # point WhatsApp account 3 at workflow 3
    manage.py qurtoba_workflow_v2 --rollback      # account back to workflow 2, drop partner overrides

    manage.py qurtoba_workflow_v2 --variant group         # build «Qurtoba Groups» (WhatsApp Web customer groups)
    manage.py qurtoba_workflow_v2 --variant group --release-group   # attach it to the WhatsApp Web account
    manage.py qurtoba_workflow_v2 --variant group --rollback-group  # detach it (the groups go quiet)

The group variant is W3's graph with the channel-neutral reply tool (qurtoba_reply_to_message — core's
whatsapp_reply_to_message only works on the Cloud API), a group section in the prompts, the «linked?»
check read from the gate (the group's customer, linked automatically when exactly one member is a
customer) and the bridge's ``group`` state declared (owner decision 2026-09-23).

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

GROUP_WORKFLOW_KEY = 'qurtoba_groups'
GROUP_WORKFLOW_NAME = 'Qurtoba Groups (WhatsApp Web)'
CLOUD_REPLY_TOOL = 'whatsapp_reply_to_message'
GROUP_REPLY_TOOL = 'qurtoba_reply_to_message'

SHARED_CORE_NODE = 'function_1783509447802'
AVAILABILITY_NODE = 'function_1780949181829'
SRC_PAYMENTS_NODE = 'agent_chat_1783507166437'
SRC_CASH_NODE = 'agent_chat_1783507168037'
SRC_NOT_LINKED_TOOL = 'tool_1781113475079'

THINKER_TOOLS = (
    'whatsapp_reply_to_message', 'qurtoba_send_customer_balance_to_chat', 'qurtoba_get_customer_daily_transactions',
    'qurtoba_check_transaction_status', 'qurtoba_check_payment_status', 'qurtoba_clear_pending_transfers',
    'alert_qurtoba_human', 'qurtoba_send_static_message',
    'qurtoba_create_new_transactions_bulk', 'qurtoba_answer_pending', 'qurtoba_request_split',
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


_OFF_HOURS_PROMPT_PATH = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, 'prompts', 'agents', 'off_hours', 'prompt.md')

# The off-hours agent can send the balance, send the statement and write quoted replies. Nothing else:
# no tool that creates, repeats, holds, cancels or checks money (owner decision 2026-09-13).
OFF_HOURS_TOOLS = (
    'qurtoba_send_customer_balance_to_chat', 'qurtoba_get_customer_daily_transactions', 'whatsapp_reply_to_message',
)


def _group_prompt(base: str, addendum_path: str) -> str:
    """W3's prompt for a customer group: the channel-neutral reply tool, and the group section inserted right
    after the context block (one source of truth — the 1:1 rules apply in the group unchanged)."""
    with open(addendum_path, encoding='utf-8') as fh:
        addendum = fh.read().strip()
    text = base.replace(CLOUD_REPLY_TOOL, GROUP_REPLY_TOOL)
    marker = '</context>'
    at = text.find(marker)
    if at < 0:
        return addendum + '\n\n' + text
    at += len(marker)
    return text[:at] + '\n\n' + addendum + '\n' + text[at:]


_GROUP_PROMPT_PATH = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, 'prompts', 'agents', 'thinker', 'group.md')
_OFF_HOURS_GROUP_PROMPT_PATH = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, 'prompts', 'agents', 'off_hours', 'group.md')


def _swap_tool(names, variant):
    return tuple(GROUP_REPLY_TOOL if (variant == 'group' and n == CLOUD_REPLY_TOOL) else n for n in names)


def _off_hours_prompt() -> str:
    with open(_OFF_HOURS_PROMPT_PATH, encoding='utf-8') as fh:
        text = fh.read()
    text = text.split('**prompt:**', 1)[1].strip() if '**prompt:**' in text else text.strip()
    for token, value in (('[[WORKING_HOURS]]', R.WORKING_HOURS), ('[[TEMPLATE_TRANSACTION]]', R.OFF_HOURS_TRANSACTION),
                         ('[[TEMPLATE_PAYMENT]]', R.OFF_HOURS_PAYMENT), ('[[TEMPLATE_STATUS]]', R.OFF_HOURS_STATUS),
                         ('[[WHEN_OPEN]]', R.OFF_HOURS_WHEN_OPEN)):
        text = text.replace(token, value)
    if '[[' in text:
        raise CommandError('off-hours prompt has an unreplaced [[placeholder]]')
    return text


def _fn(node_id, label, x, y, code, timeout=60):
    return dict(node_id=node_id, node_type='function', label=label, x=x, y=y,
                configuration={'code': code, 'description': '', 'update_state': [], 'timeout_seconds': timeout})


def build_spec(src_nodes, tool_ids, variant='cloud'):
    """(nodes, edges, global_configuration) for the v2 graph:
    AI on? [off → nothing] → linked? → route → [off-hours → closed agent | receipt → payments agent | money path → needs AI? → thinker | done]"""
    def src_cfg(node_id):
        n = src_nodes.get(node_id)
        if n is None:
            raise CommandError(f'workflow {SOURCE_WORKFLOW_ID} has no node {node_id}')
        return json.loads(json.dumps(n.configuration))

    payments = src_cfg(SRC_PAYMENTS_NODE)
    payments['handoff'] = {'enabled': False, 'targets': []}
    payments['update_state'] = []
    # owner decision 2026-09-15: Claude Haiku 4.5 main, DeepSeek V4 Flash backup (see the thinker below)
    payments['llm_model_id'] = 21
    payments['llm_model_name'] = 'Claude Haiku 4.5'
    payments['backup_llm_model_id'] = 31
    if variant == 'group':
        cloud_id, group_id = tool_ids.get(CLOUD_REPLY_TOOL), tool_ids.get(GROUP_REPLY_TOOL)
        for t in payments.get('selected_tools') or []:
            if cloud_id and t.get('tool_id') == cloud_id:
                t['tool_id'] = group_id

    thinker = src_cfg(SRC_CASH_NODE)
    thinker_text = _thinker_prompt() if variant != 'group' else _group_prompt(_thinker_prompt(), _GROUP_PROMPT_PATH)
    thinker['messages'] = [{'role': 'system', 'text': thinker_text, 'cache': True, 'cache_ttl': '5m', 'attachments': []}]
    thinker['selected_tools'] = [{'store': True, 'tool_id': tool_ids[name], 'ask_human': False}
                                 for name in _swap_tool(THINKER_TOOLS, variant)]
    # Handoff OFF (2026-09-26): the core engine runs a handoff's goto AND the thinker's own edge to
    # function_model_done in the same step; both write `message` and LangGraph aborts the run
    # (INVALID_CONCURRENT_GRAPH_UPDATE — W3 4×, W10 2× on «الصورة»; no handoff ever completed). Receipt
    # images still reach agent_payments through the router's RECEIPT intent. Re-enable only after the
    # engine routes a handoff INSTEAD of the node's edge.
    thinker['handoff'] = {'enabled': False, 'targets': []}
    thinker['update_state'] = []
    # owner decision 2026-09-15: Claude Haiku 4.5 (21) is the main model, DeepSeek V4 Flash (31) the
    # backup — DeepSeek queued every request for 900 s on 2026-09-14 22:35–23:37 (chat 13f58d64) and
    # its error never tripped the failover. (2026-09-06 had DeepSeek main: Haiku asked where to act.)
    thinker['llm_model_id'] = 21
    thinker['llm_model_name'] = 'Claude Haiku 4.5'
    thinker['backup_llm_model_id'] = 31
    thinker['max_iterations'] = 6
    thinker['max_tokens'] = 1500
    thinker['temperature'] = 0.2
    thinker['reasoning_mode'] = 'none'
    thinker['description'] = 'Thinking model: runs after the system created the clean transfers; creates only what the system could not read.'

    # Off-hours agent (manual switch «وضع خارج مواعيد العمل»): the thinker's model settings, its own prompt,
    # and ONLY the balance, statement and reply tools — nothing that can create money.
    off_hours_tools = _swap_tool(OFF_HOURS_TOOLS, variant)
    missing_off_hours_tools = [n for n in off_hours_tools if n not in tool_ids]
    if missing_off_hours_tools:
        raise CommandError(f'off-hours tools not registered: {missing_off_hours_tools}')
    off_hours = json.loads(json.dumps(thinker))
    off_text = _off_hours_prompt() if variant != 'group' else _group_prompt(_off_hours_prompt(), _OFF_HOURS_GROUP_PROMPT_PATH)
    off_hours['messages'] = [{'role': 'system', 'text': off_text, 'cache': True, 'cache_ttl': '5m', 'attachments': []}]
    off_hours['selected_tools'] = [{'store': True, 'tool_id': tool_ids[name], 'ask_human': False} for name in off_hours_tools]
    off_hours['handoff'] = {'enabled': False, 'targets': []}
    off_hours['update_state'] = []
    off_hours['description'] = ('Off-hours agent: balance and statement only; refuses every transfer, payment, status check '
                                'and cancellation with the working hours. Has no tool that can create money.')


    X0, X1, X2, X3, X4, X5, X6, X7 = 0, 320, 640, 960, 1280, 1600, 1920, 2240
    nodes = [
        # The AI switch comes first (إعدادات قرطبة › تفعيل الرد الآلي): OFF ends the turn with nothing
        # at all, no transaction, no payment, no reply (owner decision 2026-09-13).
        _fn('function_gate', 'AI switch on? (إعدادات قرطبة)', -640, 300, NODE_CODE['function_gate'], timeout=30),
        dict(node_id='conditional_ai_enabled', node_type='conditional', label='AI enabled? off: nothing runs', x=-320, y=300,
             configuration={'conditions': [{'operator': 'is_true', 'data_type': 'boolean',
                                            'variable1': '{{ function_gate.ai_enabled }}', 'variable2': ''}],
                            'default_branch': 'default'}),
        _fn('function_ai_off', 'AI off: silent, no transaction, messages marked handled', -320, 560,
            NODE_CODE['function_ai_off'], timeout=30),
        dict(node_id='conditional_linked', node_type='conditional', label='linked to a Qurtoba customer?', x=X0, y=300,
             configuration={'conditions': [{'operator': 'is_true', 'data_type': 'boolean',
                                            # a group: the gate linked (or found) the group's customer this turn
                                            'variable1': ('{{ function_gate.linked }}' if variant == 'group'
                                                          else '{{partner.has_qurtoba_customer}}'),
                                            'variable2': ''}],
                            'default_branch': 'default'}),
        _fn('function_not_linked', 'NOT LINKED: notice once, messages cancelled on arrival', X1, 560, NODE_CODE['function_not_linked']),
        _fn('function_route', 'ROUTE: off-hours switch? receipt image? else money', X1, 300, NODE_CODE['function_route']),
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
        _fn('function_off_hours_context', 'OFF-HOURS: context for the closed agent', X3, 560, NODE_CODE['function_off_hours_context']),
        dict(node_id='agent_off_hours', node_type='agent_chat',
             label='OFF-HOURS agent: balance + statement only, refuses every transaction', x=X4, y=560, configuration=off_hours),
        _fn('function_off_hours_done', 'OFF-HOURS: closed notice if silent, messages handled', X5, 560,
            NODE_CODE['function_off_hours_done']),
        dict(node_id=AVAILABILITY_NODE, node_type='function', label='service_availability', x=X3, y=40,
             configuration=src_cfg(AVAILABILITY_NODE)),
        dict(node_id=SHARED_CORE_NODE, node_type='function', label='shared_roles', x=X4, y=40,
             configuration=(src_cfg(SHARED_CORE_NODE) if variant != 'group' else {
                 **src_cfg(SHARED_CORE_NODE),
                 'code': ("def execute(input_data):\n"
                          "    from qurtoba.agent_prompts import SHARED_CORE\n"
                          f"    return SHARED_CORE.replace('{CLOUD_REPLY_TOOL}', '{GROUP_REPLY_TOOL}')\n")})),
        dict(node_id='agent_payments', node_type='agent_chat', label='payments_agent (vision model)', x=X5, y=40, configuration=payments),
    ]
    edges = [
        ('function_gate', 'conditional_ai_enabled', ''),
        ('conditional_ai_enabled', 'conditional_linked', '1'),
        ('conditional_ai_enabled', 'function_ai_off', '0'),
        ('conditional_linked', 'function_route', '1'),
        ('conditional_linked', 'function_not_linked', '0'),
        ('function_route', 'conditional_route', ''),
        ('conditional_route', AVAILABILITY_NODE, '1'),
        ('conditional_route', 'function_off_hours_context', '2'),
        ('function_off_hours_context', 'agent_off_hours', ''),
        ('agent_off_hours', 'function_off_hours_done', ''),
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
        # off-hours is the manual account switch (qurtoba.switches), not workflow state. A group run gets
        # the bridge's roster/speakers as ``state.group`` — declared, or the engine drops it.
        'state_injections': ([{'key': 'group', 'type': 'object', 'initial_value': {}, 'persist': False}]
                             if variant == 'group' else []),
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
        parser.add_argument('--variant', choices=('cloud', 'group'), default='cloud',
                            help='cloud = W3 (1:1 on the Cloud API); group = «Qurtoba Groups» (WhatsApp Web customer groups)')
        parser.add_argument('--release-group', action='store_true',
                            help='attach the group workflow to the WhatsApp Web account of the office number')
        parser.add_argument('--rollback-group', action='store_true',
                            help='detach it: the WhatsApp Web account answers no group')

    def handle(self, *args, **opts):
        from modules.aistudio.models import ToolDefinition, WorkflowDefinition, WorkflowEdge, WorkflowNode
        wf_id, src_id = opts['workflow_id'], opts['source_workflow']
        variant = opts['variant']

        if opts['release_group'] or opts['rollback_group']:
            return self._rollout_group(opts)
        if opts['canary'] or opts['release'] or opts['rollback']:
            return self._rollout(opts, wf_id, src_id)

        if variant == 'group':
            wf_id = self._group_workflow(create=not opts['dry_run'])
            if wf_id is None:
                self.stdout.write('(dry run) the group workflow does not exist yet — it would be cloned from '
                                  f'workflow {TARGET_WORKFLOW_ID}')
                wf_id = TARGET_WORKFLOW_ID
        wf = WorkflowDefinition.objects.filter(pk=wf_id).first()
        src = WorkflowDefinition.objects.filter(pk=src_id).first()
        if wf is None or src is None:
            raise CommandError(f'workflow {wf_id} or {src_id} not found')
        src_nodes = {n.node_id: n for n in WorkflowNode.objects.filter(workflow=src)}
        wanted = tuple(dict.fromkeys(THINKER_TOOLS + (GROUP_REPLY_TOOL,)))
        new_defs = _ensure_tool_definitions(wanted)
        if new_defs:
            self.stdout.write(f'tool definitions created: {new_defs}')
        tool_ids = dict(ToolDefinition.objects.filter(name__in=wanted).values_list('name', 'id'))
        missing = [t for t in _swap_tool(THINKER_TOOLS, variant) if t not in tool_ids]
        if missing:
            raise CommandError(f'tools not registered in ToolDefinition: {missing}')

        nodes, edges, gconf = build_spec(src_nodes, tool_ids, variant=variant)
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

    # ── WhatsApp Web customer groups (owner decision 2026-09-23) ─────────────────────────────────────

    def _group_workflow(self, create=True):
        """The «Qurtoba Groups» workflow id — cloned once from W3's definition row (the graph is then
        rebuilt from this spec, like W3's)."""
        from modules.aistudio.models import WorkflowDefinition
        wf = WorkflowDefinition.objects.filter(key=GROUP_WORKFLOW_KEY).first()
        if wf is not None:
            return wf.pk
        if not create:
            return None
        base = WorkflowDefinition.objects.get(pk=TARGET_WORKFLOW_ID)
        wf = WorkflowDefinition.objects.get(pk=TARGET_WORKFLOW_ID)
        wf.pk = None
        wf.id = None
        wf.key = GROUP_WORKFLOW_KEY
        wf.name = GROUP_WORKFLOW_NAME
        wf.description = ('Qurtoba accountant automations for the customers\' WhatsApp GROUPS on WhatsApp Web '
                          '(one group = one customer; staff lines are context only). Built by qurtoba_workflow_v2 '
                          '--variant group.')
        wf.graph_data = base.graph_data
        wf.save()
        self.stdout.write(f'workflow {wf.pk} «{wf.name}» created from workflow {TARGET_WORKFLOW_ID}')
        return wf.pk

    def _rollout_group(self, opts):
        from modules.chat.models import Conversation
        from modules.wa_web.models import WaWebAccount
        from qurtoba.groups import twin_cloud_account
        wf_id = self._group_workflow(create=False)
        accounts = [a for a in WaWebAccount.objects.all()
                    if twin_cloud_account(a) is not None and twin_cloud_account(a).pk == WHATSAPP_ACCOUNT_ID]
        if not accounts:
            raise CommandError(f'no WhatsApp Web account with the number of WhatsApp account {WHATSAPP_ACCOUNT_ID} '
                               '— connect the number first')
        for acc in accounts:
            if opts['rollback_group']:
                WaWebAccount.objects.filter(pk=acc.pk).update(group_workflow=None, ai_in_groups=False)
                self.stdout.write(f'WhatsApp Web account {acc.pk}: group workflow detached, AI in groups OFF')
                continue
            if wf_id is None:
                raise CommandError('the group workflow does not exist — run --variant group first')
            WaWebAccount.objects.filter(pk=acc.pk).update(
                group_workflow_id=wf_id, handled_by_ai=True, ai_in_groups=True, ai_in_private=False)
            # Switching the account on does not wake the groups that already exist (core sets a group's
            # handled_by_ai only when it is created): turn on the groups linked to a Qurtoba customer.
            from django.contrib.contenttypes.models import ContentType
            linked = Conversation._base_manager.filter(
                type='wa_web', is_group=True, social_account_object_id=acc.pk,
                social_account_content_type=ContentType.objects.get_for_model(WaWebAccount),
                social_partner__qurtoba_customer__isnull=False, handled_by_ai=False)
            n = linked.update(handled_by_ai=True)
            self.stdout.write(f'WhatsApp Web account {acc.pk}: group workflow → {wf_id}; AI in groups ON, private OFF; '
                              f'{n} linked group(s) switched on')

