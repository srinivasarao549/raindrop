# We take as input an 'rd.msg.conversation' schema (for a *message*) and
# emit an 'rd.conv.summary' schema for a *conversation* (ie, for a different
# rd_key than our input document).
# Building the summary is expensive, but it means less work by the front end.
# We also record we are dependent on any of the messages themselves changing,
# so when as item is marked as 'unread', the summary changes accordingly.
import itertools

# keys are the schemas we need, values are the fields from this schema we
# include in the summary (where None means we just use the schema internally
# in this extension but don't emit it in the summary)
src_msg_schemas = {
    'rd.msg.body': '''body_preview from from_display to to_display cc
                      cc_display bcc bcc_display timestamp'''.split(),
    'rd.msg.conversation': None,
    'rd.msg.archived': None,
    'rd.msg.deleted': None,
    'rd.msg.seen': ['seen'],
    'rd.msg.grouping-tag': None,
    'rd.msg.email': None,
    'rd.msg.attachment-summary': None,
}

def build_summaries(to_summarize):
    for msg in to_summarize:
        msg_key = msg['rd.msg.body']['rd_key']
        summary = {'id': msg_key,
                   'schemas': {}}
        # special case - the 'attachment summary' gets its own dict item
        try:
            summary['attachments'] = msg.pop('rd.msg.attachment-summary')['attachments']
        except KeyError:
            pass
        for scid, names in src_msg_schemas.iteritems():
            if not names:
                continue
            try:
                schema = msg[scid]
            except KeyError:
                continue
            this = summary['schemas'][scid] = {}
            for name in names:
                try:
                    this[name] = schema[name]
                except KeyError:
                    continue
        yield summary

def build_summary(conv_id):
    logger.debug('rebuilding conversation %r', conv_id)
    # query to determine all messages in this convo.
    result = open_view(viewId="msg_conversation_id", key=conv_id)
    # the list of items we want.
    wanted = []
    for row in result['rows']:
        msg_key = row['value']['rd_key']
        for sch_id in src_msg_schemas:
            wanted.append((msg_key, sch_id))

    infos = [item for item in open_schemas(wanted) if item is not None]
    all_msgs = []
    # turn them into a nested dictionary.
    for msg_key, item_gen in itertools.groupby(infos, lambda info: info['rd_key']):
        new = {}
        for item in item_gen:
            if item is not None:
                new[item['rd_schema_id']] = item
        all_msgs.append(new)

    identities = set()
    from_display = []
    from_display_map = {}
    good_msgs = []
    unread = []
    latest_by_recip_target = {}
    subject = None
    groups_with_unread = set()
    groups = set()
    for msg_info in all_msgs:
        try:
            grouping_tag = msg_info['rd.msg.grouping-tag']['tag']
        except KeyError:
            grouping_tag = None
        if grouping_tag is not None:
            groups.add(grouping_tag)
        if 'rd.msg.body' not in msg_info:
            continue
        if 'rd.msg.deleted' in msg_info and msg_info['rd.msg.deleted']['deleted']:
            continue
        if 'rd.msg.archived' in msg_info and msg_info['rd.msg.archived']['archived']:
            continue
        # the message is good!
        good_msgs.append(msg_info)
        if 'rd.msg.seen' not in msg_info or not msg_info['rd.msg.seen']['seen']:
            unread.append(msg_info)
            if grouping_tag is not None:
                groups_with_unread.add(grouping_tag)
        body_schema = msg_info['rd.msg.body']            
        for field in ["to", "cc"]:
            if field in body_schema:
                for val in body_schema[field]:
                    identities.add(tuple(val))
        try:
            identities.add(tuple(body_schema['from']))
        except KeyError:
            pass # things like RSS feeds don't have a 'from'
        try:
            fd = body_schema['from_display']
            if (not fd in from_display_map):
                from_display_map[fd] = 1
                from_display.append(fd)
        except KeyError:
            pass # things like RSS feeds don't have a 'from_display'
        if 'rd.msg.grouping-tag' in msg_info:
            this_group = msg_info['rd.msg.grouping-tag']['tag']
            this_ts = msg_info['rd.msg.body']['timestamp']
            cur_latest = latest_by_recip_target.get(this_group, 0)
            if this_ts > cur_latest:
                latest_by_recip_target[this_group] = this_ts

    logger.debug('conversation has %d messages, %d good', len(all_msgs), len(good_msgs))
    # build a map of grouping-tag to group ID
    latest_by_grouping = {}
    result = open_view(viewId="grouping_info_tags",
                       keys=latest_by_recip_target.keys())
    for row in result['rows']:
        gtag = row['key']
        gkey = row['value']
        this_ts = latest_by_recip_target[gtag]
        cur_latest = latest_by_grouping.get(hashable_key(gkey), 0)
        if this_ts > cur_latest:
            latest_by_grouping[hashable_key(gkey)] = this_ts
        logger.debug('grouping-tag %r appears in grouping %r', gtag, gkey)

    # sort the messages so we can determine the first
    good_msgs.sort(key=lambda item: item['rd.msg.body']['timestamp'])
    unread.sort(key=lambda item: item['rd.msg.body']['timestamp'])

    if good_msgs:
        # We want the subject from the first (topic) message
        subject = good_msgs[0]['rd.msg.body'].get('subject')
        num_summaries = 2 # not including the first...
        # and the summary to include the first, and prefer the last 2 unread
        # but if not enough unread, use the most recent read.
        # establish this order via sorting.
        def key_fun(info):
            is_unread = 'rd.msg.seen' not in info or not info['rd.msg.seen']['seen']
            return (int(is_unread), info['rd.msg.body']['timestamp'])
        to_summarize = sorted(good_msgs[1:], key=key_fun)[-num_summaries:]
        # re-sort again based purely on timestamp.
        to_summarize.sort(key=lambda info: info['rd.msg.body']['timestamp'])
        # and the convo starter.
        to_summarize.insert(0, good_msgs[0])
    else:
        subject = None
        to_summarize = []

    item = {
        'subject': subject,
        'messages': list(build_summaries(to_summarize)),
        'message_ids': [i['rd.msg.body']['rd_key'] for i in good_msgs],
        'unread_ids': [i['rd.msg.body']['rd_key'] for i in unread],
        'identities': sorted(list(identities)),
        'from_display': from_display,
        'grouping-timestamp': [],
        'unread_grouping_tags': sorted(groups_with_unread),
        'all_grouping_tags': sorted(groups),
    }
    for target, timestamp in sorted(latest_by_grouping.iteritems()):
        item['grouping-timestamp'].append([target, timestamp])
    # our 'dependencies' are *all* messages, not just the "good" ones.
    deps = []
    for msg in all_msgs:
        # get the rd_key from any schema which exists
        msg_key = list(msg.values())[0]['rd_key']
        for sid in src_msg_schemas:
            deps.append((msg_key, sid))
    return item, deps

def handler(doc):
    rd_source = [doc['_id'], doc['_rev']]
    # ask for our 'later_handler' to be called with this
    process_later((doc['conversation_id'], rd_source))

def later_handler(infos):
    # first make a list of unique convo IDs and remembering an arbitrary
    # rd_source we can record.
    cid_src = {}
    for cid, rd_source in infos:
        cid_src[hashable_key(cid)] = rd_source

    # now build them.
    for cid, rd_source in cid_src.iteritems():
        item, deps = build_summary(cid)
        emit_schema('rd.conv.summary', item, rd_key=cid, rd_source=rd_source,
                    deps=deps)
