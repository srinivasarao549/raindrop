# This is an extension that converts a raw/message/email
# to a raw/message/email/mailing-list-extracted, i.e. an email message
# that contains information about the mailing list to which the message
# was sent, if any.

# We extract mailing list info from RFC 2369 headers, which look like this:
#
#  Mailing-List: list raindrop-core@googlegroups.com;
#      contact raindrop-core+owner@googlegroups.com
#  List-Id: <raindrop-core.googlegroups.com>
#  List-Post: <mailto:raindrop-core@googlegroups.com>
#  List-Help: <mailto:raindrop-core+help@googlegroups.com>
#  List-Unsubscribe: <http://googlegroups.com/group/raindrop-core/subscribe>,
#      <mailto:raindrop-core+unsubscribe@googlegroups.com>

# Here's another one (with some other headers that may be relevant):

#  Archived-At: <http://www.w3.org/mid/49F0D9FC.6060103@w3.org>
#  Resent-From: public-webapps@w3.org
#  X-Mailing-List: <public-webapps@w3.org> archive/latest/3067
#  Sender: public-webapps-request@w3.org
#  Resent-Sender: public-webapps-request@w3.org
#  Precedence: list
#  List-Id: <public-webapps.w3.org>
#  List-Help: <http://www.w3.org/Mail/>
#  List-Unsubscribe: <mailto:public-webapps-request@w3.org?subject=unsubscribe>

# XXX This should be a plugin that extends the message/email extension,
# like the evite and skype plugins described in Life Cycle of a Message 2 -
# Documents and States
# <http://groups.google.com/group/raindrop-core/web/life-cycle-of-a-message-2---documents-and-states>.

import re
import logging

logger = logging.getLogger(__name__)

from ...proc import base

class MailingListExtractor(base.SimpleConverterBase):
    target_type = 'msg', 'raw/message/email/mailing-list-extracted'
    sources = [('msg', 'raw/message/email')]
    def simple_convert(self, doc):
        ret = doc.copy()

        # email.py does this, and we have to do it too, or else
        # DocumentModel::prepare_ext_document throws an exception when it finds
        # these keys in the document, even though it says the requirement
        # for _id to be absent from the document is because it "manage[s] IDs
        # for all but 'raw' docs," and this is a "raw" doc as far as I can tell.
        for n in ret.keys():
            if n.startswith('_') or n.startswith('raindrop'):
                del ret[n]
        del ret['type']

        if 'list-id' not in doc['headers']:
            return ret

        logger.warning("i'm in ur pipeline xtractng ur mailng lists")

        # FIXME: use keys().filter() or an array comprehension?
        headers = filter(lambda x: x.startswith('list-'), doc['headers'].keys())
        logger.debug("list-* headers: %s", headers)

        mailing_list = {}

        # I haven't actually seen one of these list-id values that includes
        # the name of the list, but this regexp was in the old JavaScript code,
        # so we do it here too.
        match = re.search('([\W\w]*)\s*<(.+)>.*', doc['headers']['list-id'])
        if (match):
            logger.debug("complex list-id header with name '%s' and ID '%s'",
                  match.group(1), match.group(2))
            mailing_list['name']  = match.group(1)
            mailing_list['id']    = match.group(2)
        else:
            logger.debug("simple list-id header with ID '%s'",
                         doc['headers']['list-id'])
            mailing_list['id'] = doc['headers']['list-id']

        # For now just reflect the literal values of the various headers
        # into the dict; eventually we'll want to do some processing of those
        # values to make life easier on the frontend.
        # XXX reflect other list-related headers like (X-)Mailing-List
        # and Archived-At?
        for key in ['list-post', 'list-archive', 'list-help', 'list-subscribe',
                    'list-unsubscribe']:
            if key in doc['headers']:
                mailing_list[key[5:]] = doc['headers'][key]
                logger.debug("set %s to %s", key[5:], doc['headers'][key])

        ret['mailing_list'] = mailing_list

        return ret