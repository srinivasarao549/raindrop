import rfc822
import time
from pprint import pformat
from raindrop.tests import TestCaseWithCorpus, TestCaseWithTestDB

class ConvoTestMixin:

    def get_messages_in_convo(self, cid):
        result = self.doc_model.open_view(viewId="msg_conversation_id",
                                          key=cid)
        return [row['value']['rd_key'] for row in result['rows']]
    
    def put_docs(self, corpus_name, corpus_spec="*", expected=None):
        items = [d for d in self.gen_corpus_schema_items(corpus_name, corpus_spec)]
        if expected is not None:
            self.failUnlessEqual(len(items), expected)
        self.doc_model.create_schema_items(items)
        self.ensure_pipeline_complete()


class TestSimpleCorpus(TestCaseWithCorpus, ConvoTestMixin):

    def test_convo_single(self):
        # Initialize the corpus & database.
        self.init_corpus('hand-rolled')

        # Process a single item - should get its own convo
        self.put_docs('hand-rolled', 'sent-email-simple', 1)

        msgid = ['email', 'd3d08a8a534c464881a95b75300e9011@something']
        body_schema = self.doc_model.open_schemas([(msgid, 'rd.msg.body')])[0]
        # should be one 'rd.convo.summary' doc in the DB.
        key = ['schema_id', 'rd.conv.summary']
        result = self.doc_model.open_view(key=key, reduce=False,
                                          include_docs=True)
        rows = result['rows']
        self.failUnlessEqual(len(rows), 1, str(rows))
        self.failUnlessEqual(rows[0]['doc']['rd_schema_id'], 'rd.conv.summary')
        doc_sum = rows[0]['doc']
        expected_doc = {
            'message_ids': [msgid],
            'unread_ids': [msgid],
            'subject': None, # our test messages have no subject!
            'grouping-timestamp': [[['display-group', 'inflow'], body_schema['timestamp']]],
            'identities': [['email', 'raindrop_test_recip2@mozillamessaging.com'],
                           ['email', 'raindrop_test_recip3@mozillamessaging.com'],
                           ['email', 'raindrop_test_recip@mozillamessaging.com'],
                           ['email', 'raindrop_test_user@mozillamessaging.com'],
                            ],
            'from_display': ['Raindrop Test User'],
            'unread_grouping_tags': ['identity-email-raindrop_test_user@mozillamessaging.com'],
            'all_grouping_tags': ['identity-email-raindrop_test_user@mozillamessaging.com'],
        }
        del doc_sum['messages'] # delete fields we aren't testing.
        self.failUnlessDocEqual(doc_sum, expected_doc)

        # only this message should be in the convo.
        msgs = self.get_messages_in_convo(doc_sum['rd_key'])
        self.failUnlessEqual(msgs, [msgid])

    def test_convo_deleted(self):
        def filter_log(record):
            # we don't write a location record, so the extension which notices
            # we should update IMAP logs a warning.
            return record.msg.startswith("Can't find imap location for message")
        self.log_handler.ok_filters.append(filter_log)

        self.test_convo_single()
        # now make our one message 'deleted'.
        msgid = ['email', 'd3d08a8a534c464881a95b75300e9011@something']
        si = {
            'rd_key': msgid,
            'rd_schema_id': 'rd.msg.deleted',
            'rd_ext_id': 'rd.testsuite',
            'items' : {
                'deleted': True,
                'outgoing_state': 'outgoing',
            }
        }
        self.doc_model.create_schema_items([si])
        self.ensure_pipeline_complete()
        # should be one 'rd.convo.summary' doc in the DB.
        key = ['schema_id', 'rd.conv.summary']
        result = self.doc_model.open_view(key=key, reduce=False,
                                          include_docs=True)
        rows = result['rows']
        self.failUnlessEqual(len(rows), 1, str(rows))
        self.failUnlessEqual(rows[0]['doc']['rd_schema_id'], 'rd.conv.summary')
        doc_sum = rows[0]['doc']
        expected_doc = {
            'subject': None, # our test messages have no subject!
            'message_ids': [],
            'unread_ids': [],
            'identities': [],
            'from_display': [],
            'unread_grouping_tags': [],
            'all_grouping_tags': ['identity-email-raindrop_test_user@mozillamessaging.com'],
            'grouping-timestamp': [],
        }
        del doc_sum['messages'] # delete fields we aren't testing.
        self.failUnlessDocEqual(doc_sum, expected_doc)
        # Note our deleted message still has the rd.msg.conversation schema;
        # but as we tested above, it doesn't appear in the 'summary'

    def test_convo_multiple(self):
        msgid = ['email', 'd3d08a8a534c464881a95b75300e9011@something']
        msgid_reply = ['email', '78cb2eb5dbc74cdd9691dcfdb266d1b9@something']
        self.test_convo_single()
        # add the reply.
        self.put_docs('hand-rolled', 'sent-email-simple-reply', 1)

        body_orig, body_reply = self.doc_model.open_schemas(
                                    [(msgid, 'rd.msg.body'),
                                     (msgid_reply, 'rd.msg.body'),
                                        ])
        # should be exactly one convo referencing both messages.
        key = ['schema_id', 'rd.conv.summary']
        result = self.doc_model.open_view(key=key, reduce=False,
                                          include_docs=True)
        rows = result['rows']
        self.failUnlessEqual(len(rows), 1, str(rows))
        self.failUnlessEqual(rows[0]['doc']['rd_schema_id'], 'rd.conv.summary')
        doc_sum = rows[0]['doc']
        expected_doc = {
            'unread_ids': [msgid, msgid_reply],
            'message_ids': [msgid, msgid_reply],
            # The first message in the conv is used for the subject - and
            # that message has no subject in our corpus
            'subject': None,
            'grouping-timestamp': [
                                  [['display-group', 'inflow'], body_reply['timestamp']],
                                ],
            'identities': [['email', 'raindrop_test_recip2@mozillamessaging.com'],
                           ['email', 'raindrop_test_recip3@mozillamessaging.com'],
                           ['email', 'raindrop_test_recip@mozillamessaging.com'],
                           ['email', 'raindrop_test_user@mozillamessaging.com'],
                            ],
            'from_display': ['Raindrop Test Recipient', 'Raindrop Test User'],
            'unread_grouping_tags': ['identity-email-raindrop_test_user@mozillamessaging.com'],
            'all_grouping_tags': ['identity-email-raindrop_test_user@mozillamessaging.com'],
        }
        del doc_sum['messages'] # delete fields we aren't testing.
        self.failUnlessDocEqual(doc_sum, expected_doc)
        # check messages in the convo.
        msgs = self.get_messages_in_convo(doc_sum['rd_key'])
        self.failUnlessEqual(sorted(msgs), sorted([msgid_reply, msgid]))

    def test_attach(self):
        self.init_corpus('hand-rolled')

        # Process a single item - should get its own convo
        self.put_docs('hand-rolled', 'quoted-hyperlinks', 1)

        msgid = ['email', '20090514020118.C33915681F2D@example2.com']
        # should be one 'rd.convo.summary' doc in the DB.
        key = ['schema_id', 'rd.conv.summary']
        result = self.doc_model.open_view(key=key, reduce=False,
                                          include_docs=True)
        rows = result['rows']
        self.failUnlessEqual(len(rows), 1, pformat(rows))
        doc = rows[0]['doc']
        self.failUnless('messages' in doc, doc.keys())
        # expecting only the one test message
        self.failUnlessEqual(len(doc['messages']), 1)
        msg = doc['messages'][0]
        self.failUnless('attachments' in msg, pformat(msg.keys()))
        # for now just check the 'bitly' link in the test message.
        expected = {
            'id': ['attach', [msgid, 'http://bit.ly/HQFyP']],
            'schemas': {'rd.attach.link': None,
                        'rd.attach.link.expanded': None},
        }
        self.failUnless(expected in msg['attachments'], pformat(doc))


# the following tests don't use the corpos, they just introduce a few
# 'simple' messages manually.
class TestConvCombine(TestCaseWithTestDB, ConvoTestMixin):

    msg_template = """\
Delivered-To: raindrop_test_user@mozillamessaging.com
From: Raindrop Test User <Raindrop_test_user@mozillamessaging.com>
To: Raindrop Test Recipient <Raindrop_test_recip@mozillamessaging.com>
Date: %(date)s
Message-Id: %(mid)s
References: %(refs)s

Hello
"""

    def get_message_schema_item(self, msgid, refs, date=None):
        args = {'mid': '<'+msgid+'>',
                'refs': ' '.join(['<'+ref+'>' for ref in refs]),
                'date': rfc822.formatdate(date),
                }
        src = self.msg_template % args
        si = {'rd_key': ['email', msgid],
              'rd_schema_id': 'rd.msg.rfc822',
              'rd_source' : None,
              'rd_ext_id': 'rd.testsuite',
              'items': {},
              'attachments' : {
                    'rfc822': {
                        'data': src
                    }
              }
        }
        return si

    def test_convo_combine(self):
        # in this test we introduce 2 docs with no references to each other
        # (so end up with 2 convos), then introduce a 3rd with a reference
        # to both originals - we should result in a single conversation.
        base_msg_ids = ["1234@something", "5678@something"]
        for msgid in base_msg_ids:
            si = self.get_message_schema_item(msgid, [])
            self.doc_model.create_schema_items([si])
        self.ensure_pipeline_complete()
        # should be 2 convos.
        key = ['schema_id', 'rd.conv.summary']
        result = self.doc_model.open_view(key=key, reduce=False)
        self.failUnlessEqual(len(result['rows']), 2)
        # now the last message - one convo should vanish.
        si = self.get_message_schema_item("90@something", base_msg_ids)
        self.doc_model.create_schema_items([si])
        self.ensure_pipeline_complete()
        # should be 1 convo.
        key = ['schema_id', 'rd.conv.summary']
        result = self.doc_model.open_view(key=key, reduce=False)
        self.failUnlessEqual(len(result['rows']), 1)
        conv_id = result['rows'][0]['value']['rd_key']
        # with all 3 messages.
        msg_ids = self.get_messages_in_convo(conv_id)
        mine = [['email', '90@something'],
                ['email', '1234@something'],
                ['email', '5678@something']]
        self.failUnlessEqual(sorted(msg_ids), sorted(mine))

    def test_convo_combine_lots(self):
        # much like the above test, but with more than 1 'extra' conversation
        # which needs to be merged.
        base_msg_ids = ["12@something", "34@something", '56@something']
        for msgid in base_msg_ids:
            si = self.get_message_schema_item(msgid, [])
            self.doc_model.create_schema_items([si])
        self.ensure_pipeline_complete()
        # should be 3 convos.
        key = ['schema_id', 'rd.conv.summary']
        result = self.doc_model.open_view(key=key, reduce=False)
        self.failUnlessEqual(len(result['rows']), 3)
        # now the last message - two convos should vanish.
        si = self.get_message_schema_item("78@something", base_msg_ids)
        self.doc_model.create_schema_items([si])
        self.ensure_pipeline_complete()
        # should be 1 convo.
        key = ['schema_id', 'rd.conv.summary']
        result = self.doc_model.open_view(key=key, reduce=False)
        self.failUnlessEqual(len(result['rows']), 1)
        conv_id = result['rows'][0]['value']['rd_key']
        # with all 4 messages.
        msg_ids = self.get_messages_in_convo(conv_id)
        mine = [['email', '12@something'],
                ['email', '34@something'],
                ['email', '56@something'],
                ['email', '78@something']]
        self.failUnlessEqual(sorted(msg_ids), sorted(mine))

    def test_convo_non_delivery(self):
        # in this test we introduce a "sent" message, and a
        # non-delivery-report for that message - they should appear in the
        # same convo.
        msg1 = """\
From: Raindrop Test User <Raindrop_test_user@mozillamessaging.com>
To: Raindrop Test Recipient <Raindrop_test_recip@mozillamessaging.com>
Date: Sat, 21 Jul 2009 12:13:14 -0000
Message-Id: <1234@something>

Hello
"""
        msg2 = """\
Delivered-To: raindrop_test_user@mozillamessaging.com
Received: by 10.90.81.19 with SMTP id e19cs3488agb;
        Mon, 27 Jul 2009 21:04:20 -0700 (PDT)
Return-Path: <>
Message-ID: <00163646cc282790b3046fbc2ac7@googlemail.com>
From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
To: raindrop_test_user@mozillamessaging.com
Subject: Delivery Status Notification (Failure)
Date: Mon, 27 Jul 2009 21:04:18 -0700 (PDT)
X-Failed-Recipients: Raindrop_test_recip@mozillamessaging.com

This is an automatically generated Delivery Status Notification

Delivery to the following recipient failed permanently:

     Raindrop_test_recip@mozillamessaging.com

Technical details of permanent failure: 
Google tried to deliver your message, blah blah blah

   ----- Original message -----

Received: by 10.114.103.9 with SMTP id a9mr11144409wac.14.1248753856901;
        Mon, 27 Jul 2009 21:04:16 -0700 (PDT)
Message-ID: <1234@something>
Date: Tue, 28 Jul 2009 14:03:22 +1000
From: Raindrop Test User <raindrop_test_user@mozillamessaging.com>
To: Raindrop_test_recip@mozillamessaging.com
Content-Type: text/plain; charset=ISO-8859-1; format=flowed
Content-Transfer-Encoding: 7bit


   ----- End of message -----

"""
        mid1 = "1234@something"
        mid2 = "00163646cc282790b3046fbc2ac7@googlemail.com"
        for (src, msgid) in [(msg1, mid1),
                             (msg2, mid2)]:
            si = {'rd_key': ['email', msgid],
                  'rd_schema_id': 'rd.msg.rfc822',
                  'rd_source' : None,
                  'rd_ext_id': 'rd.testsuite',
                  'items': {},
                  'attachments' : {
                        'rfc822': {
                            'data': src
                        }
                  }
                  }
            self.doc_model.create_schema_items([si])
        self.ensure_pipeline_complete()
        # should be 1 convo.
        key = ['schema_id', 'rd.conv.summary']
        result = self.doc_model.open_view(key=key, reduce=False,
                                          include_docs=True)
        self.failUnlessEqual(len(result['rows']), 1)
        # and it should have both messages.
        doc = result['rows'][0]['doc']
        self.failUnlessEqual(sorted(doc['message_ids']),
                             sorted([['email', mid1], ['email', mid2]]))


    def test_convo_order_seen(self):
        msg_ids = ["1@something", "2@something", "3@something", "4@something"]
        dates = [time.time()+x for x in range(4)]
        items = []
        for msgid, date in zip(msg_ids, dates):
            si = self.get_message_schema_item(msgid, [msg_ids[0]], date)
            items.append(si)
        # mark all except the second as 'seen'
        for msgid in [msg_ids[0]] + msg_ids[2:]:
            si = {'rd_key': ['email', msgid],
                  'rd_schema_id': 'rd.msg.seen',
                  'rd_source' : None,
                  'rd_ext_id': 'rd.testsuite',
                  'items': {'seen': True,
                            'outgoing_state': 'sent',},
                  }
            items.append(si)
        self.doc_model.create_schema_items(items)
        self.ensure_pipeline_complete()
        # Now get the convo summary - the summary messages should include
        # the first, second and last (second preferred over 3rd as it is
        # unread, last included as it is most-recent read)
        key = ['schema_id', 'rd.conv.summary']
        result = self.doc_model.open_view(key=key, reduce=False,
                                          include_docs=True)
        self.failUnlessEqual(len(result['rows']), 1)
        # and it should have both messages.
        doc = result['rows'][0]['doc']
        found_keys = [m['id'] for m in doc['messages']]
        expected_ids = [msg_ids[0], msg_ids[1], msg_ids[3]]
        expected_keys = [['email', mid] for mid in expected_ids]
        self.failUnlessEqual(found_keys, expected_keys)
