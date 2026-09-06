"""Delete every message of a conversation except the newest one (a test-line reset).

    manage.py qurtoba_clean_chat <conversation_id>            # keep the last message
    manage.py qurtoba_clean_chat <conversation_id> --keep 5   # keep the last 5
    manage.py qurtoba_clean_chat <conversation_id> --dry-run  # count only

Removes the message rows with their reactions, seen-marks and attachment links, in one
transaction. Ledger records are never touched (their link to the message becomes empty).
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = 'Delete all messages of a conversation except the newest N (default 1).'

    def add_arguments(self, parser):
        parser.add_argument('conversation_id')
        parser.add_argument('--keep', type=int, default=1, help='how many newest messages to keep (default 1)')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        from modules.chat.models import Conversation, Message
        conv = Conversation.objects.filter(id=opts['conversation_id']).first()
        if conv is None:
            raise CommandError(f'conversation {opts["conversation_id"]} not found')
        keep_ids = list(Message.objects_all.filter(conversation=conv)
                        .order_by('-created_at').values_list('id', flat=True)[:max(0, opts['keep'])])
        qs = Message.objects_all.filter(conversation=conv).exclude(id__in=keep_ids)
        n = qs.count()
        if opts['dry_run']:
            self.stdout.write(f'{conv.id}: would delete {n} messages, keep {len(keep_ids)}')
            return
        with transaction.atomic():
            qs.delete()
        self.stdout.write(f'{conv.id}: deleted {n} messages, kept {len(keep_ids)}')
