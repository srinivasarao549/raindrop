# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 1.1
#
# The contents of this file are subject to the Mozilla Public License Version
# 1.1 (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
# http://www.mozilla.org/MPL/
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License
# for the specific language governing rights and limitations under the
# License.
#
# The Original Code is Raindrop.
#
# The Initial Developer of the Original Code is
# Mozilla Messaging, Inc..
# Portions created by the Initial Developer are Copyright (C) 2009
# the Initial Developer. All Rights Reserved.
#
# Contributor(s):
#

import logging
from email.utils import mktime_tz, parsedate_tz
import time
import re
import threading
import socket
import errno
import Queue

import sys
import imapclient
import imaplib

from ..proc import base
from ..model import DocumentSaveError
from . import xoauth

brat = base.Rat

logger = logging.getLogger(__name__)

# Set this to see IMAP lines printed to the console.
# NOTE: lines printed may include your password!
#imaplib.Debug = 9

NUM_QUERYERS = 3
NUM_FETCHERS = 3

# we fetch this many bytes or this many messages, whichever we hit first.
MAX_BYTES_PER_FETCH = 500000
MAX_MESSAGES_PER_FETCH = 30

from imapclient.imap_utf7 import encode as encode_imap_utf7
from imapclient.imap_utf7 import decode as decode_imap_utf7

def log_exception(msg, *args):
  # this made more sense when things were twisted :)
  logger.exception(msg, *args)

def get_rdkey_for_email(msg_id):
  # message-ids must be consistent everywhere we use them, and we decree
  # the '<>' is stripped (if for no better reason than the Python email
  # package's 'unquote' function will strip them by default...
  if msg_id.startswith("<") and msg_id.endswith(">"):
    msg_id = msg_id[1:-1]
  return ("email", msg_id)

# Finding an imap ENVELOPE structure with non-character data isn't good -
# couch can't store it (except in attachments) and we can't do anything with
# it anyway.  It *appears* from the IMAP spec that only 7bit data is valid,
# so that is what we check
# XXX - maybe utf-7 is what we want?
def check_envelope_ok(env):
  # either strings, or (nested) lists of strings.
  def flatten(what):
    ret = []
    for item in what:
      if item is None:
        pass
      elif isinstance(item, (str, int, long)):
        ret.append(str(item))
      elif isinstance(item, (list, tuple)):
        ret.extend(flatten(item))
      else:
        raise TypeError, item
    return ret

  for item in flatten(env):
    try:
      item.encode('ascii')
    except UnicodeError:
      return False
  return True


class IMAP4AuthException(Exception):
  def __init__(self, why, *args):
    self.why = why
    Exception.__init__(self, *args)


def create_connection(address, timeout):
    # stolen from python 2.6...
    msg = "getaddrinfo returns an empty list"
    host, port = address
    for res in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        af, socktype, proto, canonname, sa = res
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            sock.settimeout(timeout)
            sock.connect(sa)
            return sock

        except socket.error, msg:
            if sock is not None:
                sock.close()

    raise socket.error, msg

# Yuck yuck yuck - monkeypatch imaplib...
def mp_open(self, host = '', port = imaplib.IMAP4_PORT):
  # overridden for timeout management...
  self.host = host
  self.port = port
  self.sock = create_connection((host, port), self.connection_timeout)
  self.sock.settimeout(self.response_timeout)
  self.file = self.sock.makefile('rb')

imaplib.IMAP4.open = mp_open

# monkey-patch imaplib's SSL readline implementation to get around
# http://bugs.python.org/issue5949
# This was fixed in Python 2.6.? and 3.x, so in theory we should check if
# the python version needs the patch or not...
def ssl_imap_readline(self):
  """Read line from remote."""
  line = []
  while 1:
      char = self.sslobj.read(1)
      line.append(char)
      if char in ("\n", ''): return ''.join(line)

imaplib.IMAP4_SSL.readline = ssl_imap_readline
  

class ImapProvider(object):
  # The 'id' of this extension
  # XXX - should be managed by our caller once these 'protocols' become
  # regular extensions.
  rd_extension_id = 'proto.imap'

  def __init__(self, account, conductor, options):
    self.account = account
    self.options = options
    self.conductor = conductor
    self.doc_model = account.doc_model
    # We have a couple of queues to do the work
    self.query_queue = Queue.Queue() # IMAP folder etc query requests 
    self.fetch_queue = Queue.Queue()
    self.updated_folder_infos = None

  def write_items(self, items):
    try:
      if items:
        self.conductor.provide_schema_items(items)
    except DocumentSaveError, exc:
      # So - conflicts are a fact of life in this 'queue' model: we check
      # if a record exists and it doesn't, so we queue the write.  By the
      # time the write gets processed, it may have been written by a
      # different extension...
      conflicts = []
      for info in exc.infos:
        if info['error']=='conflict':
          # The only conflicts we are expecting are creating the rd.msg.rfc822
          # schema, which arise due to duplicate message IDs (eg, an item
          # in 'sent items' and also the received copy).  Do a 'poor-mans'
          # check that this is indeed the only schema with a problem...
          # Note however our callers may themselves handle their own conflicts
          if not info.get('id', '').endswith('!rd.msg.rfc822'):
            raise
          conflicts.append(info)
        else:
          raise
      if not conflicts:
        raise # what error could this be??
      # so, after all the checking above, a debug log is all we need for this
      logger.debug('ignored %d conflict errors writing this batch (first 3=%r)',
                   len(conflicts), conflicts[:3])

  def maybe_queue_fetch_items(self, folder_path, infos):
    if not infos:
      return
    by_uid = self._findMissingItems(folder_path, infos)
    if not by_uid:
      return
    self.fetch_queue.put((False, self._processFolderBatch, (folder_path, by_uid)))

  def _reqList(self, conn, *args, **kwargs):
    self.account.reportStatus(brat.EVERYTHING, brat.GOOD)
    acct_id = self.account.details.get('id','')
    caps = conn.capabilities()
    if 'XLIST' in caps:
      result = conn.xlist_folders('', '*')
      kind = self.account.details.get('kind','')
      if kind is '':
        logger.warning("set kind=gmail for account %s in your .raindrop for correct settings",
                        acct_id)
    else:
      logger.warning("This IMAP server doesn't support XLIST, so performance may suffer")
      result = conn.list_folders('', '*')
    # quickly scan through the folders list building the ones we will
    # process and the order.
    logger.info("examining folders")
    folders_use = []
    # First pass - filter folders we don't care about.
    if 'exclude_folders' in self.account.details:
      to_exclude = set(o.lower() for o in re.split(", *", self.account.details['exclude_folders']))
    else:
      to_exclude = set()
    for flags, delim, name in result:
      name = decode_imap_utf7(name)
      ok = True
      for flag in (r'\Noselect', r'\AllMail', r'\Trash', r'\Spam'):
        if flag in flags:
          logger.debug("'%s' has flag %r - skipping", name, flag)
          ok = False
          break
      if ok and self.options.folders and \
         name.lower() not in [o.lower() for o in self.options.folders]:
        logger.debug('skipping folder %r - not in specified folder list', name)
        ok = False
      if ok and 'exclude_folders' in self.account.details and \
         name.lower() in to_exclude:
        logger.debug('skipping folder %r - in exclude list', name)
        ok = False
      if ok:
        folders_use.append((flags, delim, name ))

    # Second pass - prioritize the folders into the order we want to
    # process them - 'special' ones first in a special order, then remaining
    # top-level folders the order they appear, then sub-folders in the order
    # they appear...
    todo_special_folders = []
    todo_top = []
    todo_sub = []

    if 'XLIST' in caps:
      for flags, delim, name in folders_use:
        folder_info = (delim, name)
        # see if this is a special folder 
        for flag in flags:
          if flag == r'\Inbox':
            # Don't use the localized inbox name when talking to the server.
            # Gmail doesn't like this, for example.
            todo_special_folders.insert(0, (delim, "INBOX"))
            break
          elif flag in (r'\Sent', r'\Drafts'):
            todo_special_folders.append(folder_info)
            break
        else:
          # for loop wasn't broken - not a special folder
          if delim in name:
            todo_sub.append(folder_info)
          else:
            todo_top.append(folder_info)
    else:
      # older imap server - just try and find the inbox.
      for flags, delim, name in folders_use:
        folder_info = (delim, name)
        if delim in name:
          todo_sub.append(folder_info)
        elif name.lower()=='inbox':
          todo_top.insert(0, folder_info)
        else:
          todo_top.append(folder_info)
    
    todo = todo_special_folders + todo_top + todo_sub
    try:
      self._updateFolders(conn, todo)
    except:
      log_exception("Failed to update folders for account %r", acct_id)
    # and tell the query queue everything is done.
    self.query_queue.put(None)

  def _checkQuickRecent(self, conn, folder_path, max_to_fetch):
    logger.debug("_checkQuickRecent for %r", folder_path)
    # XXX - imapclient doesn't expose 'examine'
    conn.select_folder(folder_path, True)
    nitems = conn.search("((OR UNSEEN (OR RECENT FLAGGED))"
                         " UNDELETED SMALLER 50000)")
    if not nitems:
      logger.debug('folder %r has no quick items', folder_path)
      return
    nitems = nitems[-max_to_fetch:]
    batch = "%s:%s" % (nitems[0], nitems[-1])
    results = conn.fetch(batch, ("FLAGS", "INTERNALDATE", "RFC822.SIZE", "ENVELOPE"))
    for uid, result in results.iteritems():
      result['UID'] = uid
      result['INTERNALDATE'] = result['INTERNALDATE'].isoformat()
    logger.info('folder %r has %d quick items', folder_path, len(results))
    # Make a simple list.
    infos = [results[seq] for seq in sorted(int(k) for k in results)
             if self.shouldFetchMessage(results[seq])]
    self.maybe_queue_fetch_items(folder_path, infos)

  def _updateFolders(self, conn, all_names):
    # Fetch all state cache docs for all mailboxes in one go.
    # XXX - need key+schema here, but we don't use multiple yet.
    acct_id = self.account.details.get('id')
    startkey = ['key', ['imap-mailbox', [acct_id]]]
    endkey = ['key', ['imap-mailbox', [acct_id, {}]]]
    results = self.doc_model.open_view(startkey=startkey,
                                       endkey=endkey, reduce=False,
                                       include_docs=True)
    # build a map of the docs keyed by folder-name.
    caches = {}
    for row in results['rows']:
      doc = row['doc']
      folder_name = doc['rd_key'][1][1]
      if doc['rd_schema_id'] == 'rd.core.error':
        # ack - failed last time for some reason - skip it.
        continue
      assert doc['rd_schema_id'] in ['rd.imap.mailbox-cache',
                                     'rd.core.error'], doc ## fix me above
      caches[folder_name] = doc
    logger.debug('opened cache documents for %d folders', len(caches))

    # We used to do a 'quick fetch' when the expectation was that a user
    # would be sitting there waiting for the first sync.  Now that we don't
    # expect that, we don't bother doing it as it just takes more time in
    # total to finalize the full sync...
    #
    # All folders without cache docs get the special 'fetch quick'
    # treatment...
#    for delim, name in all_names:
#      if name not in caches:
#        self.query_queue.put((False, self._checkQuickRecent, (name, 20)))

    # We only update the cache of the folder once all items from that folder
    # have been written, so extensions only run once all items fetched.
    assert not self.updated_folder_infos
    self.updated_folder_infos = []

    seen = set()
    for delim, name in all_names:
      seen.add(name)
      cache_doc = caches.get(name, {})
      self.query_queue.put((False, self._updateFolderFromCache, (cache_doc, delim, name)))
    # Now the folders which we saw once before but can't see now - it must
    # have been deleted, so we remove the location records for those folders.
    missing = set(caches) - seen
    for folder_name in missing:
      logger.debug('updating folder locations for deleted folder %r', folder_name)
      self.writeLocationInfos(folder_name, None, [], [])

  def _updateFolderFromCache(self, conn, cache_doc, folder_delim, folder_name):
    # Now queue the updates of the folders
    acct_id = self.account.details.get('id')
    info = conn.select_folder(folder_name, True)
    logger.debug("info for %r is %r", folder_name, info)

    dirty = self._syncFolderCache(conn, folder_name, info, cache_doc)

    if dirty:
      logger.debug("need to update folder cache for %r", folder_name)
      items = {'uidvalidity': cache_doc['uidvalidity'],
               'infos': cache_doc['infos']
               }
      new_item = {'rd_key' : ['imap-mailbox', [acct_id, folder_name]],
                  'rd_schema_id': 'rd.imap.mailbox-cache',
                  'rd_ext_id': self.rd_extension_id,
                  'items': items,
      }
      if '_id' in cache_doc:
        new_item['_id'] = cache_doc['_id']
        new_item['_rev'] = cache_doc['_rev']
      self.updated_folder_infos.append(new_item)
      sync_items = cache_doc['infos']
    else:
      sync_items = cache_doc.get('infos')

    todo = sync_items[:]
    queued_keys = []
    while todo:
      # do later ones first and limit the batch size - larger batches means
      # fewer couch queries, but the queue appears to 'stall' for longer.
      batch = []
      while len(batch) < 100 and todo:
          mi = todo.pop()
          if self.shouldFetchMessage(mi):
              batch.insert(0, mi)
              queued_keys.append(get_rdkey_for_email(mi['ENVELOPE'][-1]))

      logger.log(1, 'queueing check of %d items in %r', len(batch), folder_name)
      self.maybe_queue_fetch_items(folder_name, batch)
    self.writeLocationInfos(folder_name, folder_delim, sync_items, queued_keys)

  def _syncFolderCache(self, conn, folder_path, server_info, cache_doc):
    # Queries the server for the current state of a folder.  Returns True if
    # the cache document was updated so needs to be written back to couch.
    suidv = int(server_info['UIDVALIDITY'])
    dirty = False
    if suidv != cache_doc.get('uidvalidity'):
      infos = cache_doc['infos'] = []
      cache_doc['uidvalidity'] = suidv
      dirty = True
    else:
      try:
        infos = cache_doc['infos']
      except KeyError:
        infos = cache_doc['infos'] = []
        dirty = True

    if infos:
      cached_uid_next = int(infos[-1]['UID']) + 1
    else:
      cached_uid_next = 1

    suidn = int(server_info.get('UIDNEXT', -1))

    if suidn == -1 or suidn > cached_uid_next:
      if suidn == -1:
        logger.warn("This IMAP server doesn't provide UIDNEXT - it will take longer to synch...")
      logger.debug('requesting info for items in %r from uid %r', folder_path,
                   cached_uid_next)
      batch = "%d:*" % (cached_uid_next,)
      new_infos = conn.fetch(batch, ("FLAGS", "INTERNALDATE", "RFC822.SIZE", "ENVELOPE"))
      for uid, result in new_infos.iteritems():
        result['UID'] = uid
        result['INTERNALDATE'] = result['INTERNALDATE'].isoformat()
    else:
      logger.info('folder %r has no new messages', folder_path)
      new_infos = {}
    # Get flags for all 'old' messages.
    if cached_uid_next > 1:
      updated_flags = conn.fetch("1:%d" % (cached_uid_next-1,), ("FLAGS",))
    else:
      updated_flags = {}
    logger.info("folder %r has %d new items, %d flags for old items",
                folder_path, len(new_infos), len(updated_flags))

    # Turn the dicts back into the sorted-by-UID list it started as, nuking
    # old messages
    infos_ndx = 0
    for this_uid in sorted(int(k) for k in updated_flags):
      info = updated_flags[this_uid]
      # remove items which no longer exist.
      while int(infos[infos_ndx]['UID']) < this_uid:
        old = infos.pop(infos_ndx)
        logger.debug('detected a removed imap item %r', old)
        dirty = True
      if int(infos[infos_ndx]['UID']) == this_uid:
        old_flags = infos[infos_ndx].get('FLAGS')
        new_flags = info["FLAGS"]
        if old_flags != new_flags:
          dirty = True
          infos[infos_ndx]['FLAGS'] = new_flags
          logger.debug('new flags for UID %r - were %r, now %r',
                       this_uid, old_flags, new_flags)
        infos_ndx += 1
        # we might get more than we asked for - that's OK - we should get
        # them in 'new_infos' too.
        if infos_ndx >= len(infos):
          break
      else:
        # We see this happen when we previously rejected an item due to
        # invalid or missing ENVELOPE etc.
        logger.debug("message %r never seen before - probably invalid", this_uid)
        continue
    # Records we had in the past now have accurate flags; anything in past
    # our current index must have been deleted.
    while infos_ndx < len(infos):
      infos.pop()
      dirty = True
    # next up is to append new message infos we just received...
    for this_uid in sorted(int(k) for k in new_infos):
      info = new_infos[this_uid]
      # Sadly, asking for '900:*' in gmail may return a single item
      # with UID of 899 - and that is already in our list.  So only append
      # new items when they are > then what we know about.
      if this_uid < cached_uid_next:
        continue
      # Some items from some IMAP servers don't have an ENVELOPE record, and
      # lots of later things get upset at that.  It isn't clear what such
      # items are yet...
      try:
        envelope = info['ENVELOPE']
      except KeyError:
        logger.debug('imap item has no envelope - skipping: %r', info)
        continue
      if envelope[-1] is None:
        logger.debug('imap item has no message-id - skipping: %r', info)
        continue
      if not check_envelope_ok(envelope):
        logger.debug('imap info has invalid envelope - skipping: %r', info)
        continue
      # it is good - keep it.
      cached_uid_next = this_uid + 1
      infos.append(info)
      dirty = True
    return dirty

  def writeLocationInfos(self, folder_name, folder_delim, sync_items, queued_keys):
    # fetch folder info location info and write it out.  As each rd_key gets
    # one record with all locations, there is the possibility a conflict will
    # happen as another folder tries to update itself too - so we loop
    # handling conflicts and retrying the read/update of the location record.
    for i in range(5):
      loc_updates = self._makeLocationInfos(folder_name, folder_delim,
                                            sync_items, queued_keys)
      if loc_updates:
        try:
          # We use update_documents as create_schema_items has issues
          # regarding conflict detection.
          self.doc_model.update_documents(loc_updates)
        except DocumentSaveError, exc:
          # Could optimize this by only retrying the ones which failed, but
          # the retry will not re-provide the ones that did worked (although
          # it will need to query when it otherwise wouldn't)
          for info in exc.infos:
            if info['error']!='conflict':
              raise
          # and retry
          logger.info('found conflict updating location document for %r - will retry',
                      folder_name)
          continue
      break
    else:
      # every retry failed
      logger.error("failed to recover from conflicts writing msg locations")

  def _makeLocationInfos(self, folder_name, delim, results, queued_keys):
    # The general process is:
    # * Query location records for all items which say they are in this location.
    # * Find the set of messages no longer in this location and remove them from
    #   this location.  Ditto for ones which we now see are in this location.
    # This function returns the 2 maps - the caller does the delete/update...
    logger.debug("checking what we know about items in folder %r", folder_name)
    acct_id = self.account.details.get('id')
    # Build a map keyed by the rd_key of all items we know are currently on
    # the IMAP server.
    current_imap = {}
    for result in results:
      if '\\deleted' not in (f.lower() for f in result['FLAGS']):
        msg_id = result['ENVELOPE'][-1]
        rdkey = get_rdkey_for_email(msg_id)
        current_imap[tuple(rdkey)] = result['UID']

    # fetch all things in couch which are currently tagged with this location
    this_location = [acct_id, folder_name]
    existing = self.doc_model.open_view(viewId='msg_location_by_source',
                                        key=this_location)
    to_nuke = []
    current_couch_locs = set()
    for row in existing['rows']:
      rdkey = tuple(row['value']['rd_key'])
      if rdkey not in current_imap:
        to_nuke.append(row['id'])
      current_couch_locs.add(rdkey)

    # Find the new ones we need to add.  This is the set of all items
    # on the IMAP server minus the ones which already have location records,
    # minus the ones which don't have rfc822 schemas in the couch (ie, those
    # we excluded due to --max-age or similar)
    missing_locs = set(current_imap) - current_couch_locs
    have_schemas = set(queued_keys)
    # check the difference between what is missing and what we know does exist.
    keys = list(missing_locs-have_schemas)
    look = [(k, 'rd.msg.rfc822') for k in keys]
    schemas = self.doc_model.open_schemas(look, include_docs=False)
    for sch, k in zip(schemas, keys):
      if sch is not None:
        have_schemas.add(k)
    # the records we need to add are the intersection of missing and have
    to_add_keys = missing_locs.intersection(have_schemas)

    to_add = []
    to_add_extra = []
    for rdkey in to_add_keys:
      # Item in the folder but couch doesn't know it is there.
      uid = current_imap[rdkey]
      si = {'rd_key': rdkey,
            'rd_schema_id': 'rd.msg.imap-locations'}
      did = self.doc_model.get_doc_id_for_schema_item(si)
      to_add.append(did)
      to_add_extra.append((rdkey, uid))

    # Open the documents we care about and update them in memory ready to
    # be written back.
    to_up = []
    docs = iter(self.doc_model.open_documents_by_id(to_nuke+to_add))
    for did in to_nuke:
      doc = next(docs)
      if doc is None:
        logger.error("looking to remove location from %s but doc doesn't exist",
                     did)
      else:
        # look for this location.
        new_locs = []
        for loc in doc['locations']:
          if loc['account']!=acct_id or loc['folder_name']!=folder_name:
            new_locs.append(loc)
        if len(doc['locations'])==len(new_locs):
          logger.error("looking to remove %s from doc %r but not in list",
                       this_location, did)
        else:
          doc['locations'] = new_locs
          logger.debug("update for %r removed a location from rev %s - now %s",
                       folder_name, doc['_rev'], new_locs)
          to_up.append(doc)
    # and the ones to add.
    for did, (rdkey, uid) in zip(to_add, to_add_extra):
      doc = next(docs)
      loc = {'folder_name': folder_name,
             'folder_delim': delim,
             'uid': uid,
             'account': acct_id,
             }
      if doc is None:
        # need to create one.
        doc = {
          'rd_key': rdkey,
          'rd_schema_id': 'rd.msg.imap-locations',
          'rd_schema_items': {
            self.rd_extension_id: {
              'rd_source': None,
              'schema': None
            },
          },
          'rd_schema_provider': self.rd_extension_id,
          'locations' : [loc],
        }
        doc['_id'] = did
        logger.debug("update for %r creating new doc", folder_name)
        to_up.append(doc)
      else:
        # doc exists - just need to add ours.
        logger.debug("update for %r updating doc rev %r (%s)", folder_name, doc['_rev'], doc['locations'])
        doc['locations'].append(loc)
        to_up.append(doc)
    logger.debug('finished making %d location infos for %r', len(to_up), folder_name)
    return to_up

  def _findMissingItems(self, folder_name, results):
    # Transform a list of IMAP infos into a map with the results keyed by the
    # 'rd_key' (ie, message-id)
    assert results, "don't call me with nothing to do!!"
    msg_infos = {}
    for msg_info in results:
      msg_id = msg_info['ENVELOPE'][-1]
      if msg_id in msg_infos:
        # This isn't a very useful check - we are only looking in a single
        # folder...
        logger.warn("Duplicate message ID %r detected", msg_id)
        # and it will get clobbered below :(
      msg_infos[get_rdkey_for_email(msg_id)] = msg_info

    # Find all messages that already have this schema
    rdkeys = msg_infos.keys()
    existing = self.doc_model.open_schemas(([k, 'rd.msg.rfc822'] for k in rdkeys),
                                           include_docs=False)
    seen = set(rdkey for (rdkey, e) in zip(rdkeys, existing) if e is not None)
    # convert each key elt to a list like we get from the views.
    remaining = set(msg_infos)-set(seen)

    logger.debug("batch for folder %s has %d messages, %d new", folder_name,
                len(msg_infos), len(remaining))
    rem_uids = [int(msg_infos[k]['UID']) for k in remaining]
    # *sob* - re-invert keyed by the UID.
    by_uid = {}
    for key, info in msg_infos.iteritems():
      uid = int(info['UID'])
      if uid in rem_uids:
        info['RAINDROP_KEY'] = key
        by_uid[uid] = info
    return by_uid

  def _processFolderBatch(self, conn, folder_path, by_uid):
    """Called asynchronously by a queue consumer"""
    conn.select_folder(folder_path, True) # should check if it already is selected?
    acct_id = self.account.details.get('id')
    num = 0
    # fetch most-recent (highest UID) first...
    left = sorted(by_uid.keys(), reverse=True)
    while left:
      # do as many as we can each time while staying inside our MAX_*
      # constraints...
      nbytes = 0
      this = []
      while left and len(this) < MAX_MESSAGES_PER_FETCH and nbytes < MAX_BYTES_PER_FETCH:
        look = left.pop(0)
        this.append(look)
        try:
          this_bytes = int(by_uid[look]['RFC822.SIZE'])
        except (KeyError, ValueError):
          logger.info("invalid message size in`%r", by_uid[look])
          this_bytes = 100000 # whateva...
        nbytes += this_bytes
      logger.debug("starting fetch of %d items from %r (%d bytes)",
                   len(this), folder_path, nbytes)
      to_fetch = ",".join(str(v) for v in this)
      results = conn.fetch(to_fetch, ("BODY.PEEK[]",))
      logger.debug("fetch from %r got %d", folder_path, len(results))
      #results = conn.fetchMessage(to_fetch, uid=True)
      # Run over the results stashing in our by_uid dict.
      infos = []
      for uid, info in results.iteritems():
        flags = by_uid[uid]['FLAGS']
        rdkey = by_uid[uid]['RAINDROP_KEY']
        content = info['BODY[]']
        mid = rdkey[-1]
        # XXX - we need something to make this truly unique.
        logger.debug("new imap message %r (flags=%s)", mid, flags)
  
        # put our schemas together
        attachments = {'rfc822' : {'content_type': 'message',
                                   'data': content,
                                   }
        }
        infos.append({'rd_key' : rdkey,
                      'rd_ext_id': self.rd_extension_id,
                      'rd_schema_id': 'rd.msg.rfc822',
                      'items': {},
                      'attachments': attachments,})
      num += len(infos)
      self.write_items(infos)
    return num

  def shouldFetchMessage(self, msg_info):
    if "\\deleted" in [f.lower() for f in msg_info['FLAGS']]:
      logger.debug("msg is deleted - skipping: %r", msg_info)
      return False
    if self.options.max_age:
      # XXX - we probably want the 'internal date'...
      date_str = msg_info['ENVELOPE'][0]
      try:
        date = mktime_tz(parsedate_tz(date_str))
      except (ValueError, TypeError):
        return False # invalid date - skip it.
      if date < time.time() - self.options.max_age:
        logger.log(1, 'skipping message - too old')
        return False
    if not msg_info['ENVELOPE'][-1]:
      logger.debug("msg has no message ID - skipping: %r", msg_info)
      return False
    return True


class ImapUpdater:
  def __init__(self, account, conductor):
    self.account = account
    self.conductor = conductor
    self.doc_model = account.doc_model

  # Outgoing items related to IMAP - eg, \\Seen flags, deleted, etc...
  def handle_outgoing(self, conductor, src_doc, to_update):
    account = self.account
    # Establish a connection to the server
    client = get_connection(account, conductor)
    # Write the fact we are about to try and (un-)set the flag(s)
    account._update_sent_state(src_doc, 'sending')
    for loc in to_update:
      logger.debug("setting flags for folder %(folder)r, uuid %(uid)s",
                   loc)
      client.select_folder(loc['folder'])
      try:
        try:
          flags_add = loc['flags_add']
        except KeyError:
          pass
        else:
          client.add_flags(loc['uid'], flags_add)
        try:
          flags_rem = loc['flags_remove']
        except KeyError:
          pass
        else:
          client.remove_flags(loc['uid'], flags_rem)
      except client.Error, exc:
        logger.error("Failed to update flags: %s", exc)
        # XXX - we need to differentiate between a 'fatal' error, such as
        # when the message has been deleted, or a transient error which can be
        # retried.  For now, assume retryable...
        account._update_sent_state(src_doc, 'error', 'imap error', str(exc),
                                             outgoing_state='outgoing')
      else:
        account._update_sent_state(src_doc, 'sent')
        logger.debug("successfully adjusted flags for %(rd_key)r", src_doc)
    client.logout()
    return True

def failure_to_status(exc):
  what = brat.SERVER
  duration = brat.TEMPORARY
  if isinstance(exc, socket.error) and exc.errno==errno.ECONNREFUSED:
    why = brat.UNREACHABLE
  elif isinstance(exc, IMAP4AuthException):
    what = brat.ACCOUNT
    why = exc.why
    # It would be nice to treat authentication failures as permanent, but
    # it isn't clear how to differentiate a "bad password" response (which
    # is permanent) from a 'too many concurrent connections' type response
    # which is temporary - so we assume all are temporary.
  elif isinstance(exc, socket.timeout):
    why = brat.TIMEOUT
  elif isinstance(exc, imapclient.IMAPClient.Error):
    logger.warn('unexpected IMAP error: %s', exc)
    why = brat.UNKNOWN
  else:
    logger.exception('unexpected exception')
    why = brat.UNKNOWN
  return {'what': what,
          'state': brat.BAD,
          'why': why,
          'duration': duration,
          'message': str(exc)}

def _do_get_connection(account, conductor):
  details = account.details
  host = details.get('host')
  is_gmail = details.get('kind')=='gmail'
  if not host and is_gmail:
    host = 'imap.gmail.com'
  if not host:
    raise ValueError, "this account has no 'host' configured"

  ssl = details.get('ssl')
  if ssl is None and is_gmail:
    ssl = True
  port = details.get('port')
  if not port:
    port = 993 if ssl else 143

  if details.get('crypto') == 'TLS':
    # a few options exist here, but none of them great.
    # Notably, the "TLS Lite" package and http://bugs.python.org/issue4471
    raise RuntimeError, "hrm - need TLS support"
  logger.debug('attempting to connect to %s:%d (ssl: %s)', host, port, ssl)
  # oh man, this sucks...
  cto = details.get('timeout_connect', account.def_timeout_connect)
  rto = details.get('timeout_response', account.def_timeout_response)
  imaplib.IMAP4.connection_timeout = cto
  imaplib.IMAP4.response_timeout = rto
  ret = imapclient.IMAPClient(host, port, ssl=ssl)
  do_oauth = ret.has_capability('AUTH=XOAUTH')
  if do_oauth:
    if not xoauth.AcctInfoSupportsOAuth(details):
      logger.warn("This server supports OAUTH but no tokens or secrets are available to use - falling back to password")
      do_oauth = False
    else:
      logger.info("logging into account %r via oauth", details['id'])

  if do_oauth:
    xoauth_string = xoauth.GenerateXOauthStringFromAcctInfo('imap', details)
    try:
      ret._imap.authenticate('XOAUTH', lambda x: xoauth_string)
    except ret.Error, exc:
      raise IMAP4AuthException(account.OAUTH, exc.args[0])
  else:
    if 'username' not in details or 'password' not in details:
      raise ValueError, "Account has no username or password"
    # XXX - is this encoding of 'username' correct?  We've already determined
    # experimentally it is *not* correct for the password...
    try:
      ret.login(encode_imap_utf7(details['username']), details['password'])
    except ret.Error, exc:
      raise IMAP4AuthException(account.PASSWORD, exc.args[0])
  account.reportStatus(brat.EVERYTHING, brat.GOOD)
  return ret

RETRYABLE_EXCEPTIONS = (imaplib.IMAP4.abort, imaplib.IMAP4.error,
                        socket.timeout, socket.error, IMAP4AuthException)

def get_connection(account, conductor):
  acct_id = account.details['id']

  def _on_connection_failed(exc):
    account.reportStatus(**failure_to_status(exc))
    if not isinstance(exc, RETRYABLE_EXCEPTIONS):
      raise
    logger.info("Failed to get a connection for '%s' - will retry: %s",
                acct_id, exc)

  return conductor.apply_with_retry(account, _on_connection_failed,
                                    _do_get_connection, account, conductor)


def drop_connection(conn):
  if conn is not None:
    try:
      conn.logout()
    except imaplib.IMAP4.error:
      # *sometimes* we get a connection lost exception trying this.
      # Is it possible gmail just aborts the connection?
      logger.debug("ignoring ConnectionLost exception when logging out")
    except Exception, exc:
      log_exception('failed to logout from the connection')


class IMAPAccount(base.AccountBase):
  rd_outgoing_schemas = ['rd.proto.outgoing.imap-flags']
  def startSend(self, conductor, src_doc, dest_doc):
    # caller should check items are ready to send.
    assert src_doc['outgoing_state'] == 'outgoing', src_doc
    # We know IMAP currently only has exactly 1 outgoing schema type.
    assert dest_doc['rd_schema_id'] == 'rd.proto.outgoing.imap-flags', src_doc
    # This one document contains *all* the accounts which need updating.
    # Sadly, multiple accounts don't really work yet :(  So for now
    # do all in *this* account and warn about the others.
    mine = []
    other = []
    for loc in dest_doc['locations']:
      if loc['account']==self.details.get('id'):
        mine.append(loc)
      else:
        other.append(loc)
    if not mine:
      return False
    if other:
      logger.warn("ignoring outgoing flags for accounts %s",
                  [l['account'] for l in other])
    updater = ImapUpdater(self, conductor)
    return updater.handle_outgoing(conductor, src_doc, mine)

  def startSync(self, conductor, options):
    done = threading.Event()
    prov = ImapProvider(self, conductor, options)

    def consume_connection_queue(q):
      """Processes the query queue."""
      acct_id = self.details['id']
      qitem = None
      context = {'conn': None}
      try:
        while True:
          qitem = q.get()
          if qitem is None:
            logger.debug('queue processor stopping')
            q.put(None) # tell other consumers to stop
            break
          seeder, func, xargs = qitem
          # a real item to process.
          # Have a connection - do the work.
          def _doit():
            if context['conn'] is None:
              context['conn'] = get_connection(self, conductor)
            args = (context['conn'],) + xargs
            func(*args)

          def _on_failure(exc):
            self.reportStatus(**failure_to_status(exc))
            if not isinstance(exc, RETRYABLE_EXCEPTIONS):
              raise
            logger.warning('Failed to process queue for %r (%s) - will retry',
                           acct_id, exc)
            drop_connection(context['conn'])
            context['conn'] = None

          try:
            conductor.apply_with_retry(self, _on_failure, _doit)
          except Exception, exc:
            # some other error, or the 'retry' failed - just skip this req
            # and continue
            drop_connection(context['conn'])
            context['conn'] = None
            self.reportStatus(**failure_to_status(exc))
            log_exception('failed to process an IMAP query request for account %r',
                          acct_id)
            if seeder:
              q.put(None)
      finally:
        drop_connection(context['conn'])

    def run_queryers(n):
      threads = []
      for i in range(n):
        t = threading.Thread(target=consume_connection_queue,
                             args=(prov.query_queue,))
        t.start()
        threads.append(t)
      # wait for them all to complete
      for t in threads:
        t.join()
      # queryers done - post to the fetch queue telling it everything is done.
      acid = self.details.get('id','')
      logger.info('%r imap querying complete - waiting for fetch queue',
                  acid)
      prov.fetch_queue.put(None)

    def run_fetchers(n):
      threads = []
      for i in range(n):
        t = threading.Thread(target=consume_connection_queue,
                             args=(prov.fetch_queue,))
        t.start()
        threads.append(t)
      for t in threads:
        t.join()
      # fetchers done - write the cache docs last.
      if prov.updated_folder_infos:
        prov.write_items(prov.updated_folder_infos)

    def start_producing(conn):
      prov._reqList(conn)

    def log_status(until_threads):
      alive_threads = until_threads[:]
      next = time.time() + 10
      while alive_threads:
        t = alive_threads[0]
        t.join(timeout=next-time.time())
        if t.isAlive():
          # timed-out.
          # A RuntimeError can happen if the queue mutates while we are
          # counting
          for i in range(5):
            try:
              nf = sum(len(i[2][1]) for i in prov.fetch_queue.queue if i is not None)
              if nf:
                logger.info('%r fetch queue has %d messages',
                            self.details.get('id',''), nf)
              break
            except RuntimeError:
              pass
          next = time.time() + 10
        else:
          # this thread has stopped.
          alive_threads.pop(0)

    # put something in the fetch queue to fire things off, noting that
    # this is the 'queue seeder' - it *must* succeed so it writes a None to
    # the end of the queue so the queue stops.  The retry semantics of the
    # queue mean that we can't simply post the None in the finally of the
    # function - it may be called multiple times.  If the queue consumer
    # gives up on a seeder function, it posts the None for us.
    prov.query_queue.put((True, start_producing, ()))

    # fire off the producer and queue consumers.
    threads = []
    threads.append(threading.Thread(target=run_queryers,
                                    args=(NUM_QUERYERS,)))
    threads.append(threading.Thread(target=run_fetchers,
                                    args=(NUM_FETCHERS,)))
    for t in threads:
      t.start()
    status_thread = threading.Thread(target=log_status, args=(threads,))
    status_thread.start()
    for t in threads:
      t.join()
    status_thread.join()

  def get_identities(self):
    addresses = self.details.get('addresses')
    if not addresses:
      username = self.details.get('username')
      if '@' not in username:
        logger.warning(
          "IMAP account '%s' specifies a username that isn't an email address.\n"
          "This account should have an 'addresses=' entry added to the config\n"
          "file with a list of email addresses to be used with this account\n"
          "or raindrop will not be able to detect items sent by you.",
          self.details['id'])
        ret = []
      else:
        ret = [['email', username]]
    else:
      ret = [['email', addy] for addy in re.split("[ ,]", addresses) if addy]
    logger.debug("email identities for %r: %s", self.details['id'], ret)
    return ret
