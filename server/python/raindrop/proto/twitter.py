#!/usr/bin/env python
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

'''
Fetch twitter raw* objects
'''

# prevent 'import twitter' finding this module!
from __future__ import absolute_import
from __future__ import with_statement

import logging
import sys
import re

from ..proc import base

# imports needed for oauth support.
from random import getrandbits
from time import time
import urllib
import hashlib
import hmac
from twitter.twitter_globals import POST_ACTIONS

import twitter
twitter.twitter_globals.POST_ACTIONS.append('retweet')
from twitter.oauth import OAuth

import calendar, rfc822

logger = logging.getLogger(__name__)

re_user = re.compile(r'@(\w+)')

def user_to_raw(user):
    return dict([('twitter_'+name.lower(), unicode(val).encode('utf-8')) \
      for name, val in user.iteritems() if val is not None])

# Used for direct messages and regular tweets
def tweet_to_raw(tweet):
    ret = {}
    # get the tweet user or the dm sender, this will error if no user, or sender
    # but that shouldn't happen to us... right?
    user = tweet.pop("user", None) or tweet.pop("sender")

    # simple hacks - users just become the screenname.
    # XXX This needs to be fixed as the screen_name can change but the id will
    # remain constant
    ret["twitter_user"] = user.get("screen_name")
    ret["twitter_user_name"] = user.get("name")

    # XXX It'd be better to keep this function in tweet-to-common but we make calls
    # for raw tweets via the this field in the API
    # fake a timestamp value we need for comparing
    ret['twitter_created_at_in_seconds'] = \
      calendar.timegm(rfc822.parsedate(tweet.get("created_at")))

    ret.update(dict([('twitter_'+name.lower(), unicode(val).encode('utf-8')) \
      for name, val in tweet.iteritems() if val is not None]))

    return ret

def create_api(account_details):
    # later we will remove all 'password' support (actually, it appears
    # twitter itself will beat us to it!)
    if 'oauth_token' not in account_details:
        username = account_details['username']
        pw = account_details['password']
        kw = {'email': username, 'password': pw}
    else:
        auth = OAuth(token=account_details['oauth_token'],
                     token_secret=account_details['oauth_token_secret'],
                     consumer_key=account_details['oauth_consumer_key'],
                     consumer_secret=account_details['oauth_consumer_secret'])
        kw = {'auth': auth}
    return twitter.api.Twitter(**kw)


class TwitterProcessor(object):
    # The 'id' of this extension
    # XXX - should be managed by our caller once these 'protocols' become
    # regular extensions.
    rd_extension_id = 'proto.twitter'
    def __init__(self, account, conductor, options):
        self.account = account
        self.options = options
        self.doc_model = account.doc_model # this is a little confused...
        self.conductor = conductor
        self.twit = None
        self.seen_tweets = None

    def go(self):
        logger.info("attaching to twitter...")
        ad = self.account.details
        twit = self.twit = create_api(ad)
        logger.info("attached to twitter - fetching timeline")

        # Lets get fancy and check our rate limit status on twitter

        # The supplied dictionary looks like this
        # rate_limit_status {
            # reset_time :              Tue Feb 23 04:22:53 +0000 2010
            # remaining_hits :          127
            # hourly_limit :            150
            # reset_time_in_seconds :   1266898973
        # }
        rate_limit_status = twit.account.rate_limit_status()

        logger.info("rate limit status: %s more hits, resets at %s",
                    rate_limit_status.get("remaining_hits"),
                    rate_limit_status.get("reset_time"))

        # Throw a warning in the logs if this user is getting close to the rate
        # limit.  Hopefully we can look for these to tune how often we should be
        # checking in with Twitter
        if rate_limit_status.get("remaining_hits", 0) < 30:
            logger.warn("Twitter is nearing the rate limit and will reset at %s",
                         rate_limit_status.reset_time)

        # If we aren't going to have enough calls lets just fail out and quit
        if rate_limit_status.get("remaining_hits", 0) < 2:
            logger.error("Your Twitter has hit the rate limit and will reset at %s",
                         rate_limit_status.reset_time)
            return

        # XXX We grab this and don't use it, but it's fun to get anyway
        me = None

        this_users = {} # probably lots of dupes
        this_items = {} # tuple of (twitter_item, rd_key, schema_id)
        keys = []

        # We could use the since_id to limit the traffic between us and Twitter
        # however it might not be worth the extra call to our systems 
        since_id = 1
        startkey = ["rd.msg.tweet.raw", "twitter_id"]
        endkey = ["rd.msg.tweet.raw", "twitter_id", 999999999999]
        results = self.doc_model.open_view(startkey=startkey, endkey=endkey,
                                           limit=1, reduce=False,
                                           descending=True,
                                           include_docs=False)
        # We grab the since_id but don't use it yet until we've got some unit
        # tests to show that this works correctly every time
        if len(results.get("rows")) > 0:
            logger.info("results %s", results.get("rows")[0].get("value").get("rd_key")[1])
            since_id = int(results.get("rows")[0].get("value").get("rd_key")[1])

        # statuses.home_timeline gets us our friends latest tweets (+retweets)
        # as well as their identity info all in a single request
        # This doesn't get us all of our friends but with 200 tweets we'll at
        # least get your most chatty friends
        tl= self.twit.statuses.home_timeline(count=200)
        for status in tl:
            id = int(status.get("id"))
            rd_key = ['tweet', str(id)]
            schema_id = 'rd.msg.tweet.raw'
            keys.append(['key-schema_id', [rd_key, schema_id]])
            this_items[id] = (status, rd_key, schema_id)
            this_users[status.get("user").get("screen_name")] = status.get("user")
            if status.get("retweeted_status", None) is not None:
                logger.info("Twitter status id: %s is a retweet", id)

        # Grab any direct messages that are waiting for us
        ml = self.twit.direct_messages()
        for dm in ml:
            id = int(dm.get("id"))
            rd_key = ['tweet-direct', str(id)]
            schema_id = 'rd.msg.tweet-direct.raw'
            keys.append(['key-schema_id', [rd_key, schema_id]])
            this_items[id] = (dm, rd_key, schema_id)
            # sender gives us an entire user dictionary for the sender so lets
            # save that for later
            if dm.get("sender_screen_name") not in this_users:
                this_users[dm.get("sender_screen_name")] = dm.get("sender")

            # this is a trick way to get our own twitter account information
            # and pop removes the duplicate data from the dm
            me = dm.pop("twitter_recipient", None)

        # execute a view to work out which of these tweets/messages are new
        # if we were using the since_id correctly this probably wouldn't be a
        # necessary step
        results = self.doc_model.open_view(keys=keys, reduce=False)
        seen_tweets = set()
        for row in results['rows']:
            seen_tweets.add(row['value']['rd_key'][1])

        infos = []
        for tid in set(this_items.keys())-set(seen_tweets):
            # create the schema for the tweet/message itself.
            item, rd_key, schema_id = this_items[tid]
            fields = tweet_to_raw(item)
            infos.append({'rd_key' : rd_key,
                          'rd_ext_id': self.rd_extension_id,
                          'rd_schema_id': schema_id,
                          'items': fields})

        # now the same treatment for the users we found; although for users
        # the fact they exist isn't enough - we also check their profile is
        # accurate.
        keys = []
        for sn in this_users.iterkeys():
            keys.append(['key-schema_id',
                         [["identity", ["twitter", sn]], 'rd.identity.twitter']])
        # execute a view process these users.
        results = self.doc_model.open_view(keys=keys, reduce=False,
                                           include_docs=True)
        seen_users = {}
        for row in results['rows']:
            _, idid = row['value']['rd_key']
            _, name = idid
            seen_users[name] = row['doc']

        # XXX - check the account user is in the list!!

        # XXX - check fields later - for now just check they don't exist.
        for sn in set(this_users.keys())-set(seen_users.keys()):
            user = this_users[sn]
            if user is None:
                # this probably shouldn't happen anymore
                logger.info("Have unknown user %r - todo - fetch me!", sn)
                continue
            items = user_to_raw(user)
            rdkey = ['identity', ['twitter', sn]]
            infos.append({'rd_key' : rdkey,
                          'rd_ext_id': self.rd_extension_id,
                          'rd_schema_id': 'rd.identity.twitter',
                          'items': items})

        if infos:
            self.conductor.provide_schema_items(infos)


class TwitterAccount(base.AccountBase):
    rd_outgoing_schemas = ['rd.msg.outgoing.tweet']

    def startSend(self, conductor, src_doc, dest_doc):
        logger.info("Sending tweet from TwitterAccount...")

        self.src_doc = src_doc
        twitter_api = create_api(self.details)
        logger.info("attached to twitter - sending tweet")

        self._update_sent_state(self.src_doc, 'sending')

        # Do the actual twitter send.
        try:
            if ('retweet_id' in self.src_doc):
                # A retweet
                retweet_id = str(self.src_doc['retweet_id'])
                status = twitter_api.statuses.retweet(id=retweet_id)
            else:
                # A status update or a reply
                extra_args = {}
                if self.src_doc.get('in_reply_to'):
                    extra_args['in_reply_to'] = self.src_doc['in_reply_to']

                status = twitter_api.statuses.update(
                               status=self.src_doc['body'], **extra_args)

            # If status has an ID, then it saved. Otherwise,
            # assume an error. TODO: store the result as a real incoming
            # schema? Probably will need to differentiate between tweets,
            # replies and retweets in those cases?
            if ("id" in status):
                # Success
                self._update_sent_state(self.src_doc, 'sent')
            else:
                # Log error
                logger.error("Twitter API status update failed: %s", status)
                self._update_sent_state(self.src_doc, 'error',
                                        'Twitter API status update failed', status,
                                        # reset to 'outgoing' if temp error.
                                        # or set to 'error' if permanent.
                                        outgoing_state='error')

        except Exception, e:
            logger.error("Twitter API status update failed: %s", str(e))
            self._update_sent_state(self.src_doc, 'error',
                                    'Twitter API failed', str(e),
                                    # reset to 'outgoing' if temp error.
                                    # or set to 'error' if permanent.
                                    outgoing_state='error')

        return True

    def startSync(self, conductor, options):
        return TwitterProcessor(self, conductor, options).go()

    def get_identities(self):
        return [('twitter', self.details['username'])]
