# We take as input an 'rd.conv.messages' schema and emit an 'rd.conv.summary'
# schema.  Building the summary is expensive, but it means less work by the
# front end.
# We also record we are dependent on any of the messages themselves changing,
# so when as item is marked as 'unread', the summary changes accordingly.
import itertools

# these are the schemas we need.
src_msg_schemas = [
    'rd.msg.body',
    'rd.msg.archived',
    'rd.msg.deleted',
    'rd.msg.seen',
    'rd.msg.recip-target',
]

def handler(doc):
    # we expect the convo to have messages or have been nuked.
    assert doc['messages']
    # the list of items we want.
    wanted = []
    for msg_key in doc['messages']:
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
    good_msgs = []
    unread = []
    latest_by_recip_target = {}
    earliest_timestamp = latest_timestamp = None
    subject = None
    for msg_info in all_msgs:
        if 'rd.msg.body' not in msg_info:
            continue
        if 'rd.msg.deleted' in msg_info and msg_info['rd.msg.deleted']['deleted']:
            continue
        if 'rd.msg.archived' in msg_info and msg_info['rd.msg.arvhived']['archived']:
            continue
        # the message is good!
        good_msgs.append(msg_info)
        if 'rd.msg.seen' not in msg_info or not msg_info['rd.msg.seen']['seen']:
            unread.append(msg_info)
        body_schema = msg_info['rd.msg.body']            
        for field in ["to", "cc"]:
            if field in body_schema:
                for val in body_schema[field]:
                    identities.add(tuple(val))
        try:
            identities.add(tuple(msg_info['rd.msg.body']['from']))
        except KeyError:
            pass # things like RSS feeds don't have a 'from'
        if earliest_timestamp is None or \
           msg_info['rd.msg.body']['timestamp'] < earliest_timestamp:
            earliest_timestamp = msg_info['rd.msg.body']['timestamp']
        if latest_timestamp is None or \
           msg_info['rd.msg.body']['timestamp'] > latest_timestamp:
            latest_timestamp = msg_info['rd.msg.body']['timestamp']
        if 'rd.msg.recip-target' in msg_info:
            this_target = msg_info['rd.msg.recip-target']['target']
            this_ts = msg_info['rd.msg.body']['timestamp']
            cur_latest = latest_by_recip_target.get(this_target, 0)
            if this_ts > cur_latest:
                latest_by_recip_target[this_target] = this_ts
        # last subject seen wins.
        this_subject = msg_info['rd.msg.body'].get('subject')
        if this_subject:
            subject = this_subject

    # a couple of 'pseudo' or 'combo' recip-targets.
    if 'direct' in latest_by_recip_target and 'group' in latest_by_recip_target:
        latest_by_recip_target['personal'] = \
            max(latest_by_recip_target['direct'], latest_by_recip_target['group'])

    if 'broadcast' in latest_by_recip_target and 'notification' in latest_by_recip_target:
        latest_by_recip_target['impersonal'] = \
            max(latest_by_recip_target['broadcast'], latest_by_recip_target['notification'])

    # sort the messages and select the 3 most-recent.
    good_msgs.sort(key=lambda item: item['rd.msg.body']['timestamp'],
                   reverse=True)
    recent = good_msgs[:3]

    item = {
        'subject': subject,
        # msg list is subtly different than rd.conv.messages - only the "good" messages.
        'message_ids': [i['rd.msg.body']['rd_key'] for i in good_msgs],
        'unread_ids': [i['rd.msg.body']['rd_key'] for i in unread],
        'identities': sorted(list(identities)),
        'earliest_timestamp': earliest_timestamp,
        'latest_timestamp': latest_timestamp,
        'target-timestamp': [],
    }
    for target, timestamp in sorted(latest_by_recip_target.iteritems()):
        item['target-timestamp'].append([target, timestamp])
    # our 'dependencies' are *all* messages, not just the "good" ones.
    deps = []
    for msg in all_msgs:
        for sid in src_msg_schemas:
            deps.append((msg['rd.msg.body']['rd_key'], sid))
    emit_schema('rd.conv.summary', item, deps=deps)
