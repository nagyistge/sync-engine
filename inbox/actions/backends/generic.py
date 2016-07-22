# -*- coding: utf-8 -*-
""" Operations for syncing back local datastore changes to
    generic IMAP providers.
"""
from collections import defaultdict
from inbox.crispin import writable_connection_pool
from nylas.logging import get_logger
from inbox.mailsync.backends.imap.generic import uidvalidity_cb
from inbox.models.backends.imap import ImapUid
from inbox.models import Folder, Category, Account, Message
from inbox.models.session import session_scope
from imaplib import IMAP4
from inbox.sendmail.base import generate_attachments
from inbox.sendmail.message import create_email
from inbox.util.misc import imap_folder_path

log = get_logger()

PROVIDER = 'generic'

__all__ = ['set_remote_starred', 'set_remote_unread', 'remote_move',
           'remote_save_draft', 'remote_delete_draft', 'remote_create_folder',
           'remote_update_folder', 'remote_delete_folder']

# STOPSHIP(emfree):
# * should update local UID state here after action succeeds, instead of
#   waiting for sync to pick it up
# * should add support for rolling back message.categories() on failure.


def uids_by_folder(message_id, db_session):
    results = db_session.query(ImapUid.msg_uid, Folder.name).join(Folder). \
        filter(ImapUid.message_id == message_id).all()
    mapping = defaultdict(list)
    for uid, folder_name in results:
        mapping[folder_name].append(uid)
    return mapping


def _create_email(account, message):
    blocks = [p.block for p in message.attachments]
    attachments = generate_attachments(blocks)
    from_name, from_email = message.from_addr[0]
    msg = create_email(from_name=from_name,
                       from_email=from_email,
                       reply_to=message.reply_to,
                       inbox_uid=message.inbox_uid,
                       to_addr=message.to_addr,
                       cc_addr=message.cc_addr,
                       bcc_addr=message.bcc_addr,
                       subject=message.subject,
                       html=message.body,
                       in_reply_to=message.in_reply_to,
                       references=message.references,
                       attachments=attachments)
    return msg


def _set_flag(account_id, message_id, flag_name, is_add):
    with session_scope(account_id) as db_session:
        uids_for_message = uids_by_folder(message_id, db_session)
    if not uids_for_message:
        log.warning('No UIDs found for message', message_id=message_id)
        return

    with writable_connection_pool(account_id).get() as crispin_client:
        for folder_name, uids in uids_for_message.items():
            crispin_client.select_folder(folder_name, uidvalidity_cb)
            if is_add:
                crispin_client.conn.add_flags(uids, [flag_name])
            else:
                crispin_client.conn.remove_flags(uids, [flag_name])


def set_remote_starred(account, message_id, starred):
    _set_flag(account, message_id, '\\Flagged', starred)


def set_remote_unread(account, message_id, unread):
    _set_flag(account, message_id, '\\Seen', not unread)


def remote_move(account_id, message_id, destination):
    with session_scope(account_id) as db_session:
        uids_for_message = uids_by_folder(message_id, db_session)
    if not uids_for_message:
        log.warning('No UIDs found for message', message_id=message_id)
        return

    with writable_connection_pool(account_id).get() as crispin_client:
        for folder_name, uids in uids_for_message.items():
            crispin_client.select_folder(folder_name, uidvalidity_cb)
            crispin_client.conn.copy(uids, destination)
            crispin_client.delete_uids(uids)


def remote_create_folder(account_id, category_id):
    with session_scope(account_id) as db_session:
        account_provider = db_session.query(Account).get(account_id).provider
        category = db_session.query(Category).get(category_id)
        display_name = category.display_name

    with writable_connection_pool(account_id).get() as crispin_client:
        # Some generic IMAP providers have different conventions
        # regarding folder names. For example, Fastmail wants paths
        # to be of the form "INBOX.A". The API abstracts this.
        if account_provider not in ['gmail', 'eas']:
            # Translate the name of the folder to an actual IMAP name
            # (e.g: "Accounting/Taxes" becomes "Accounting.Taxes")
            new_display_name = imap_folder_path(
                display_name, separator=crispin_client.folder_separator,
                prefix=crispin_client.folder_prefix)
        else:
            new_display_name = display_name
        crispin_client.conn.create_folder(new_display_name)

    if new_display_name != display_name:
        with session_scope(account_id) as db_session:
            category = db_session.query(Category).get(category_id)
            category.display_name = new_display_name


def remote_update_folder(account_id, category_id, old_name):
    with session_scope(account_id) as db_session:
        account_provider = db_session.query(Account).get(account_id).provider
        category = db_session.query(Category).get(category_id)
        display_name = category.display_name

    with writable_connection_pool(account_id).get() as crispin_client:
        if account_provider not in ['gmail', 'eas']:
            new_display_name = imap_folder_path(
                display_name, separator=crispin_client.folder_separator,
                prefix=crispin_client.folder_prefix)
        else:
            new_display_name = display_name
        crispin_client.conn.rename_folder(old_name, new_display_name)

    if new_display_name != display_name:
        with session_scope(account_id) as db_session:
            category = db_session.query(Category).get(category_id)
            category.display_name = new_display_name


def remote_delete_folder(account_id, category_id):
    with session_scope(account_id) as db_session:
        account_provider = db_session.query(Account).get(account_id).provider
        category = db_session.query(Category).get(category_id)
        display_name = category.display_name

    with writable_connection_pool(account_id).get() as crispin_client:
        try:
            if account_provider not in ['gmail', 'eas']:
                # Translate a Unix-style path to the actual folder path.
                display_name = imap_folder_path(
                    display_name, separator=crispin_client.folder_separator,
                    prefix=crispin_client.folder_prefix)

            crispin_client.conn.delete_folder(display_name)
        except IMAP4.error:
            # Folder has already been deleted on remote. Treat delete as
            # no-op.
            pass

    with session_scope(account_id) as db_session:
        category = db_session.query(Category).get(category_id)
        db_session.delete(category)
        db_session.commit()


def remote_save_draft(account_id, message_id):
    with session_scope(account_id) as db_session:
        account = db_session.query(Account).get(account_id)
        message = db_session.query(Message).get(message_id)
        mimemsg = _create_email(account, message)

    with writable_connection_pool(account_id).get() as crispin_client:
        if 'drafts' not in crispin_client.folder_names():
            log.info('Account has no detected drafts folder; not saving draft',
                     account_id=account_id)
            return
        folder_name = crispin_client.folder_names()['drafts'][0]
        crispin_client.select_folder(folder_name, uidvalidity_cb)
        crispin_client.save_draft(mimemsg)


def remote_update_draft(account_id, message_id, old_message_id_header):
    with session_scope(account_id) as db_session:
        account = db_session.query(Account).get(account_id)
        message = db_session.query(Message).get(message_id)
        message_id_header = message.message_id_header
        message_public_id = message.public_id
        version = message.version
        mimemsg = _create_email(account, message)

    # Steps to updating draft:
    # 1. Create the new message, unless it's somehow already there
    # 2. Delete the old message the API user is updating

    with writable_connection_pool(account_id).get() as crispin_client:
        if 'drafts' not in crispin_client.folder_names():
            log.info('Account has no drafts folder. Will not save draft.',
                     account_id=account_id)
            return
        folder_name = crispin_client.folder_names()['drafts'][0]
        crispin_client.select_folder(folder_name, uidvalidity_cb)
        existing_new_draft = crispin_client.find_by_header(
            'Message-Id', message_id_header)
        if not existing_new_draft:
            crispin_client.save_draft(mimemsg)
        else:
            log.info('Draft has been saved, will not create a duplicate.',
                     message_id_header=message_id_header)

        # Check for an older version and delete it. (We can stop once we find
        # one, to reduce the latency of this operation.)
        old_version_deleted = crispin_client.delete_draft(
            old_message_id_header)
        if old_version_deleted:
            log.info('Cleaned up old draft',
                     old_message_id_header=old_message_id_header,
                     message_id_header=message_id_header)


def remote_delete_draft(account_id, inbox_uid, message_id_header):
    with writable_connection_pool(account_id).get() as crispin_client:
        if 'drafts' not in crispin_client.folder_names():
            log.info(
                'Account has no detected drafts folder; not deleting draft',
                account_id=account_id)
            return
        crispin_client.delete_draft(message_id_header)


def remote_delete_sent(account_id, message_id_header, delete_multiple=False):
    with writable_connection_pool(account_id).get() as crispin_client:
        if 'sent' not in crispin_client.folder_names():
            log.info(
                'Account has no detected sent folder; not deleting message',
                account_id=account_id)
            return
        crispin_client.delete_sent_message(message_id_header, delete_multiple)


def remote_save_sent(account_id, message_id):
    with session_scope(account_id) as db_session:
        account = db_session.query(Account).get(account_id)
        message = db_session.query(Message).get(message_id)
        if message is None:
            log.info('tried to create nonexistent message',
                     message_id=message_id, account_id=account_id)
            return
        mimemsg = _create_email(account, message)

    with writable_connection_pool(account_id).get() as crispin_client:
        if 'sent' not in crispin_client.folder_names():
            log.info('Account has no detected sent folder; not saving message',
                     account_id=account_id)
            return

        folder_name = crispin_client.folder_names()['sent'][0]
        crispin_client.select_folder(folder_name, uidvalidity_cb)
        crispin_client.create_message(mimemsg)
