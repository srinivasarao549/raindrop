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

# The raindrop 'extension environment'.  Responsible for setting up all the
# globals available to extensions.
import uuid
import logging

logger = logging.getLogger(__name__)

_my_identities = []
_known_grouping_tags = set()

def reset_for_test_suite():
    _my_identities[:] = []
    _known_grouping_tags.clear()


class ProcessLaterException(Exception):
    def __init__(self, value):
        self.value = value
        Exception.__init__(self, value)

class InternalNoDepsSentinal: pass

def get_ext_env(doc_model, context, src_doc, ext):
    # Hack together an environment for the extension to run in
    # (specifically, provide the emit_schema etc globals)
    # NOTE: These are all called in the context of a worker thread and
    # are expected by the caller to block.
    new_items = context['new_items']

    def _do_deps(schema_item, deps):
        if deps is InternalNoDepsSentinal:
            pass
        elif deps is not None:
            if not ext.uses_dependencies:
                raise ValueError("extension %r is not marked as using "
                                 "dependencies" % (ext.id,))
            schema_item['rd_deps'] = deps
        else:
            if ext.uses_dependencies:
                logger.warn("Extension %r is marked as using dependencies, "
                            "but did not specify them.  Performance of this "
                            "extension will suffer.", ext.id)

    def emit_schema(schema_id, items, rd_key=None, attachments=None, deps=None):
        ni = {'rd_schema_id': schema_id,
              'items': items,
              'rd_ext_id' : ext.id,
              }
        if rd_key is None:
            ni['rd_key'] = src_doc['rd_key']
        else:
            ni['rd_key'] = rd_key
        _do_deps(ni, deps)
        ni['rd_source'] = [src_doc['_id'], src_doc['_rev']]
        if attachments is not None:
            ni['attachments'] = attachments
        if ext.category != ext.EXTENDER:
            ni['rd_schema_provider'] = ext.id
        new_items.append(ni)
        return doc_model.get_doc_id_for_schema_item(ni)

    def later_emit_schema(schema_id, items, rd_key, rd_source, attachments=None, deps=None):
        ni = {'rd_schema_id': schema_id,
              'items': items,
              'rd_ext_id' : ext.id,
              }
        ni['rd_key'] = rd_key
        _do_deps(ni, deps)
        ni['rd_source'] = rd_source
        if attachments is not None:
            ni['attachments'] = attachments
        if ext.category != ext.EXTENDER:
            ni['rd_schema_provider'] = ext.id
        new_items.append(ni)

    def emit_related_identities(identity_ids, def_contact_props):
        logger.debug("emit_related_identities for %r", ext.id)
        for item in items_from_related_identities(doc_model,
                                             identity_ids,
                                             def_contact_props,
                                             ext.id):
            item['rd_source'] = [src_doc['_id'], src_doc['_rev']]
            new_items.append(item)
        logger.debug("emit_related_identities for %r - now %d items",
                     ext.id, len(new_items))

    def find_and_emit_conversation(msg_ids):
        logger.debug("find_and_emit_conversation for %r", ext.id)
        # we use the same ext_id here regardless of the actual extension
        # calling us.
        ext_id = 'rd.core'
        for item in items_from_convo_relations(doc_model,
                                                msg_ids, ext_id):
            item['rd_source'] = [src_doc['_id'], src_doc['_rev']]
            new_items.append(item)
        logger.debug("find_and_emit_conversation for %r - now %d items",
                     ext.id, len(new_items))

    def open_schema_attachment(src, attachment, **kw):
        "A function to abstract document storage requirements away..."
        doc_id = src['_id']
        dm = doc_model
        found, info = dm.get_schema_attachment_info(src, attachment)
        logger.debug("attempting to open attachment %s/%s", doc_id, found)
        return dm.db.openDoc(dm.quote_id(doc_id), attachment=found, **kw)

    def open_attachment(doc_id, attach_id):
        "A function to abstract document storage requirements away..."
        dm = doc_model
        logger.debug("attempting to open attachment %s/%s", doc_id, attach_id)
        return dm.db.openDoc(dm.quote_id(doc_id), attachment=attach_id)

    def open_view(*args, **kw):
        context['did_query'] = True
        return doc_model.open_view(*args, **kw)

    def open_schemas(*args, **kw):
        return doc_model.open_schemas(*args, **kw)

    def update_documents(docs):
        context['did_query'] = True
        assert docs, "please fix the extension to not bother calling with no docs!"
        return doc_model.update_documents(docs)

    def get_my_identities():
        # XXX - can't use globals here - so we cheat!
        from raindrop.extenv import _my_identities
        # Some extensions need to know which identity IDs mean the current
        # user for various purposes - eg, "was it sent to/from me?".
        # We could let such extensions use open_view, but then it would
        # be flagged as a 'dynamic' extension when it isn't really.
        # So - abstract some of that behind this helper.
        # For now, assume identities don't change between runs.  Later we
        # could listen for changes to account schemas in the pipeline and
        # invalidate...
        if not _my_identities:
            result = doc_model.open_view(
                        viewId='acct_identities',
                        reduce=True,
                        group=True,
                        group_level=1,
                        )
            for row in result['rows']:
                iid = row['key'][0]
                # can't use a set - identity_ids are lists!
                if iid not in _my_identities:
                    _my_identities.append(iid)
        return _my_identities

    def init_grouping_tag(tag, grouping_key, grouping_title):
        # Tell the system that a new 'grouping tag' has sprung into life.  Used
        # for things like mailing-lists which need to create new groupings
        # at runtime.
        # If the grouping-tag is already associated with a different rd.grouping
        # schema, no action is taken.
        # this simple cache makes a big improvement!
        # XXX - can't use globals here - so we cheat!
        from raindrop.extenv import _known_grouping_tags
        if tag in _known_grouping_tags:
            return
        
        result = doc_model.open_view(viewId='grouping_info_tags', key=tag)
        if result['rows']:
            _known_grouping_tags.add(tag)
            return # this grouping already exists.
        # create a new 'info' schema marked as 'dynamic' so it can be deleted
        # when no items exist in it.
        items = {'title': grouping_title,
                 'dynamic': True,
                 'grouping_tags': [tag]}
        emit_schema('rd.grouping.info', items, rd_key=grouping_key,
                    deps=InternalNoDepsSentinal)
        _known_grouping_tags.add(tag)

    def process_later(info):
        raise ProcessLaterException(info)

    new_globs = {}
    if src_doc:
        new_globs['emit_schema'] = emit_schema
    else:
        new_globs['emit_schema'] = later_emit_schema
    new_globs['emit_related_identities'] = emit_related_identities
    new_globs['find_and_emit_conversation'] = find_and_emit_conversation
    new_globs['init_grouping_tag'] = init_grouping_tag
    new_globs['open_attachment'] = open_attachment
    new_globs['open_schema_attachment'] = open_schema_attachment
    new_globs['open_schemas'] = open_schemas
    new_globs['process_later'] = process_later
    new_globs['get_schema_attachment_info'] = doc_model.get_schema_attachment_info
    new_globs['open_view'] = open_view
    new_globs['update_documents'] = update_documents
    new_globs['get_my_identities'] = get_my_identities
    new_globs['hashable_key'] = doc_model.hashable_key
    new_globs['logger'] = logging.getLogger('raindrop.ext.'+ext.id)
    return new_globs


def items_from_related_identities(doc_model, idrels, def_contact_props, ext_id):
    idrels = list(idrels) # likely a generator...
    assert idrels, "don't give me an empty list - just don't emit!!"
    if __debug__: # check the extension is sane...
        for iid, rel in idrels:
            assert isinstance(iid, (tuple, list)) and len(iid)==2,\
                   repr(iid)
            assert rel is None or isinstance(rel, basestring), repr(rel)

    # Take a short-cut to ensure all identity records exist and to
    # handle conflicts from the same identity being created at the
    # same time; ask the doc-model to emit a NULL schema for each
    # one if it doesn't already exist.
    for iid, rel in idrels:
        yield {'rd_key' : ['identity', iid],
               'rd_schema_id' : 'rd.identity.exists',
               'items': None,
               'rd_ext_id' : ext_id,
               }

    # Find contacts associated with any and all of the identities;
    # any identities not associated with a contact will be updated
    # to have a contact (either one we find with for different ID)
    # or a new one we create.
    # XXX - can we safely do this in parallel?
    wanted = []
    for iid, rel in idrels:
        # the identity itself.
        rdkey = ['identity', iid]
        wanted.append((rdkey, 'rd.identity.contacts'))

    results = doc_model.open_schemas(wanted)

    assert len(results)==len(idrels), (results, idrels)

    # scan the list looking for an existing contact for any of the ids.
    for schema in results:
        if schema is not None:
            contacts = schema.get('contacts', [])
            if contacts:
                contact_id = contacts[0][0]
                logger.debug("Found existing contact %r", contact_id)
                break
    else: # for loop not broken...
        # We expect a 'displayName' field at least...
        assert 'displayName' in def_contact_props, def_contact_props
        # See if we can match the 'displayName' for a contact.
        result = doc_model.open_view(viewId="contact_name",
                                     key=def_contact_props['displayName'])
        if result['rows']:
            rdkey = result['rows'][0]['value']['rd_key']
            assert rdkey[0]=="contact", rdkey # not a contact?
            contact_id = rdkey[1]
        else:
            # allocate a new contact-id; we can't use a 'natural key' for a
            # contact....
            contact_id = str(uuid.uuid4())
            # just choose any of the ID's details (although first is likely
            # to be 'best')
            cdoc = {}
            cdoc.update(def_contact_props)
            logger.debug("Will create new contact %r", contact_id)
            yield {'rd_key' : ['contact', contact_id],
                   'rd_schema_id' : 'rd.contact',
                   # ext_id might be wrong here - maybe it should be 'us'?
                   'rd_ext_id' : ext_id,
                   'items' : cdoc,
            }

    # We know the contact to use and the list of identities
    # which we know exist. We've also got the 'contacts' schemas for
    # those identities - which may or may not exist, and even if they do,
    # may not refer to this contact.  So fix all that...
    for schema, (iid, rel) in zip(results, idrels):
        # identity ID is a tuple/list of exactly 2 elts.
        assert isinstance(iid, (tuple, list)) and len(iid)==2, repr(iid)
        new_rel = (contact_id, rel)
        doc_id = doc_rev = None # incase we are updating a doc...
        if schema is None:
            # No 'contacts' schema exists for this identity
            new_rel_fields = {'contacts': [new_rel]}
        else:
            existing = schema.get('contacts', [])
            logger.debug("looking for %r in %s", contact_id, existing)
            for cid, existing_rel in existing:
                if cid == contact_id:
                    new_rel_fields = None
                    break # yay
            else:
                # not found - we need to update this doc
                new_rel_fields = schema.copy()
                schema['contacts'] = existing + [new_rel]
                logger.debug("new relationship (update) from %r -> %r",
                             iid, contact_id)
                # and note the fields which allows us to update...
                doc_id = schema['_id']
                doc_rev = schema['_rev']
        if new_rel_fields is not None:
            yield {'rd_key' : ['identity', iid],
                   'rd_schema_id' : 'rd.identity.contacts',
                   'rd_ext_id' : ext_id,
                   'items' : new_rel_fields,
            }

# XXX - this is very close logically to items_from_related_identities - it
# needs to be refactored!
def items_from_convo_relations(doc_model, msg_keys, ext_id):
    # We look for an existing convo with any of the messages.  If we don't
    # find one, we create a new one.  This is to handle email, which does
    # not have the concept of a canonical conversation - a conversation is
    # "derived" from related messages.  This should not be used for services
    # which provide an ID for a conversation - eg, skyke has the concept of a "chat" and messages within that chat.
    # In cases like the above, a simple rd.msg.conversation schema can be
    # emitted.
    msg_keys = list(msg_keys) # likely a generator...
    assert msg_keys, "don't give me an empty list - just don't emit!!"

    # Find conversations associated with any and all of the messages;
    all_conv_keys = set()
    existing = {}
    got = doc_model.open_schemas([rdkey, 'rd.msg.conversation']
                                 for rdkey in msg_keys)
    for rdkey, e in zip(msg_keys, got):
        if e is not None:
            cid = doc_model.hashable_key(e['conversation_id'])
            existing[doc_model.hashable_key(rdkey)] = cid
            all_conv_keys.add(cid)

    # see if an existing convo exists for these messages.
    if len(all_conv_keys) == 0:
        # make a new one; the conv_id will include the entire rd_key
        # of one of the messages to avoid conflicts between different
        # 'providers' (eg, while a msg-id should be unique within emails,
        # there is nothing to prevent a 'skype chat ID' conflicting with
        # a message-id.)
        conv_id = ['conv', msg_keys[0]]
    else:
        # at least 1 convo - and possibly more (in which case we update the
        # other convos to point at this convo)
        conv_id = list(all_conv_keys)[0]

    conv_id = doc_model.hashable_key(conv_id)
    convos_to_merge = set()
    # now run over all the keys we were passed and see which ones need updating.
    for msg_key in msg_keys:
        msg_key = doc_model.hashable_key(msg_key)
        try:
            if existing[msg_key] != conv_id:
                convos_to_merge.add(existing[msg_key])
        except KeyError:
            # no existing convo for this message - easy
            yield {'rd_key': msg_key,
                   'rd_schema_id': 'rd.msg.conversation',
                   'rd_ext_id': ext_id,
                   'rd_schema_provider': ext_id,
                   'items': {'conversation_id': conv_id}}
    # find all existing items in all convos to merge, and update every message
    # in those convos to point at this one.
    results = doc_model.open_view(viewId="msg_conversation_id",
                                  keys=list(convos_to_merge))
    for row in results['rows']:
        yield {'rd_key': row['value']['rd_key'],
               'rd_schema_id': 'rd.msg.conversation',
               'rd_ext_id': ext_id,
               'rd_schema_provider': ext_id,
               '_rev': row['value']['_rev'],
               'items': {'conversation_id': conv_id}}
