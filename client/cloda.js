/**
 * Cloda, it's a play on gloda and cloud.  Get it?!
 */

var GlodaConversationProto = {
  threadMessages: function() {
    var messages = this.messages;
    var messageIdMap = {}, message, i, ref, parent, children;
    for (var i = 0; i < messages.length; i++) {
      message = messages[i];
      messageIdMap[message.header_message_id] = message;
    }
    // now find their closest parent...
    for each (message in messageIdMap) {
      // references are ordered from old (0) to new (n-1), so walk backwards
      for (var iRef = message.references.length-1; iRef >= 0; iRef--) {
        ref = message.references[iRef];
        if (ref in messageIdMap) {
          // link them to their parent
          parent = messageIdMap[ref];
          message._parent = parent;
          children = parent._children;
          if (children === undefined)
            children = parent._children = [];
          children.push(message);
          break;
        }
      }
    }
    // return list of parent-less nodes, sorted by date
    var topnodes = [];
    for each (message in messageIdMap) {
      if (!message._parent) {
        topnodes.push(message);
      }
    }
    topnodes.sort(function (a,b) { return a.timestamp - b.timestamp; } );
    return topnodes;
  },
  get subject() {
    return this.messages[0].subject();
  },
};

var GlodaMessageProto = {
  subject: function() {
    return this.headers["Subject"];
  },
  _bodyTextHelper: function(aPart) {
    var result = "";
    if (aPart.parts) {
      return aPart.parts.map(this._bodyTextHelper, this).join("");
    }
    else if (aPart.contentType == "text/plain")
      return aPart.data;
    else
      return "";
  },
  // something scary happens when these are getters in terms of putting things back
  bodyText: function() {
    return this._bodyTextHelper(this.bodyPart);
  },
  bodySnippet: function() {
    return this.bodyText().substring(0, 128);
  },
  _rawSetDefault: function(aKey, aDefaultValue) {
    var raw = this.__proto__;
    console.log("this", this, "raw", raw);
    if (aKey in raw) {
      var val = raw[aKey];
      if (val != null)
        return val;
    }
    return (raw[aKey] = aDefaultValue);
  },
  addTag: function(aTagName) {
    var tags = this._rawSetDefault("tags", []);
    tags.push(aTagName);
    this.save();
  },
  save: function() {
    // 'this' is actually a "phantom" protecting the raw message from
    //  our convenience additions or foolish accidental mutations.  What we
    //  want to persist is the raw message sans prototype, so we take the proto
    //  out of the equation for the JSON snapshot period.
    try {
      var raw_message = this.__proto__;
      raw_message.__proto__ = undefined;
      delete raw_message.__proto__;
      Gloda.dbMessages.saveDoc(raw_message);
    }
    catch (e) {
      console.log("problem when saving...", e);
    }
    raw_message.__proto__ = GlodaMessageProto;
  }
};

function GlodaConvQuery() {
}
GlodaConvQuery.prototype = {
  /**
   * Issue a query given a set of constraints where each constraint query will
   *  return a set of conversations.  We intersect those sets in order to 
   *  get the list of conversations we will actually load and return.
   */
  queryForConversations: function (aConstraints, aCallback, aCallbackThis) {
    this.constraints = aConstraints;
    this.callback = aCallback;
    this.callbackThis = aCallbackThis;

    var dis = this;
    this.wrappedProcessResults = function() {
      dis.processResults.apply(dis, arguments);
    };
    this.seenConversations = null;

    this.constraintsPending = aConstraints.length;
    this.constraints.forEach(this.dispatchConstraint, this);
  },
  dispatchConstraint: function(aConstraint) {
    var viewName = aConstraint.view;
    delete aConstraint.view;
    aConstraint["success"] = this.wrappedProcessResults;
    Gloda.dbMessages.view(viewName, aConstraint);
  },
  /**
   * Result handling function for constraints issued by queryForConversations.
   *  Each result set has rows whose values are conversation ids.  Once all
   *  constraints have return their results, we load the conversations
   *  by a call to getConversations.
   */
  processResults: function(result) {
    var nextSeen = {}, rows = result.rows, iRow, row, conversationId;
    if (this.seenConversations == null) {
      for (iRow = 0; iRow < rows.length; iRow++) {
        row = rows[iRow];
        nextSeen[row.value] = true;
      }
    }
    else {
      for (iRow = 0; iRow < rows.length; iRow++) {
        conversationId = rows[iRow].value;
        if (conversationId in this.seenConversations)
          nextSeen[conversationId] = true;
      }
    }
    this.seenConversations = nextSeen;
    console.log("processResults", this.seenConversations);
    if (--this.constraintsPending == 0) {
      var conversationIds = [];
      for (conversationId in this.seenConversations) {
        conversationIds.push(conversationId);
      }
      this.getConversations(conversationIds, this.callback, this.callbackThis);
    }
  },
  /**
   * Retrieve conversations by id, also loading any involved contacts and
   *  reflecting them onto the messages themselves.
   */
  getConversations: function(aConversationIds, aCallback, aCallbackThis) {
    this.callback = aCallback;
    this.callbackThis = aCallbackThis;

    var dis = this;
    Gloda.dbMessages.view("by_conversation/by_conversation", {
      keys: aConversationIds, include_docs: true,
      success: function(result) {
        dis.processConversationFetch(result);
      }
    });
  },
  processConversationFetch: function(result) {
    // we receive the list of fetched messages.  we need to group them by
    //  conversation (this should be trivially easy because they should come
    //  back grouped, but we're not going to leverage that.)
    // we also need to get the list of distinct contact id's seen so that we
    //  can look up the contacts.
    var conversations = this.conversations = {};
    var seenContactIds = {};
    var rows = result.rows, iRow, row, contact_id;
    for (iRow = 0; iRow < rows.length; iRow++) {
      row = rows[iRow];
      var message = row.doc;
      var conversation = conversations[message.conversation_id];
      if (conversation === undefined)
        conversation = conversations[message.conversation_id] = {
          __proto__: GlodaConversationProto,
          id: message.conversation_id,
          oldest: message.timestamp, newest: message.timestamp,
          involves_contact_ids: {}, raw_messages: []
        };
      conversation.raw_messages.push(message);
      if (conversation.oldest > message.timestamp)
        conversation.oldest = message.timestamp;
      if (conversation.newest < message.timestamp)
        conversation.newest = message.timestamp;
      for (var iContactId = 0; iContactId < message.involves_contact_ids.length;
           iContactId++) {
        contact_id = message.involves_contact_ids[iContactId];
        conversation.involves_contact_ids[contact_id] = true;
        seenContactIds[contact_id] = true;
      }
    }

    console.log("seenContactIds", seenContactIds);
    var contact_ids = [];
    for (contact_id in seenContactIds)
      contact_ids.push(contact_id);

    console.log("contact lookup list:", contact_ids);

    var dis = this;
    Gloda.dbContacts.allDocs({ keys: contact_ids, include_docs: true,
      success: function(result) {
        dis.processContactFetch(result);
      }
    });
  },
  processContactFetch: function(result) {
    // --- receive the contacts, translate them into the messages...
    // -- build the contact map
    var rows = result.rows, iRow, row, contact;
    var contacts = {};
    for (iRow = 0; iRow < rows.length; iRow++) {
      row = rows[iRow];
      contact = row.doc;
      contacts[contact._id] = contact;
    }

    function mapContactMap(aMap) {
      var out = [];
      for (var key in aMap)
        out.push(contacts[key]);
      return out;
    }
    function mapContactList(aList) {
      var out = [];
      for (var i = 0; i < aList.length; i++) {
        out.push(contacts[aList[i]]);
      }
      return out;
    }

    // -- process the conversations
    var convList = [];
    for each (var conversation in this.conversations) {
      conversation.involves = mapContactMap(conversation.involves_contact_ids);
      convList.push(conversation);

      wrapped_messages = [];
      for (var iMsg = 0; iMsg < conversation.raw_messages.length; iMsg++) {
        var raw_message = conversation.raw_messages[iMsg];
        raw_message.__proto__ = GlodaMessageProto;
        var message = {__proto__: raw_message};
        message.from = contacts[message.from_contact_id];
        message.to = mapContactList(message.to_contact_ids);
        message.cc = mapContactList(message.cc_contact_ids);
        message.involves = mapContactList(message.involves_contact_ids);
        
        wrapped_messages.push(message);
      }
      conversation.messages = wrapped_messages;
      conversation.messages.sort(function (a,b) {return a.timestamp - b.timestamp;});
    }

    convList.sort(function (a, b) { return b.newest - a.newest; });
    console.log("callback with conv list", convList);
    this.callback.call(this.callbackThis, convList);
  }
};

var MAX_TIMESTAMP = 4000000000;

var Gloda = {
  dbContacts: $.couch.db("contacts"),
  dbMessages: $.couch.db("messages"),

  _init: function () {

  },

  queryByInvolved: function(aInvolvedContactIds, aCallback, aCallbackThis) {
    // -- for each involved person, get the set of conversations they're in
    var constraints = aInvolvedContactIds.map(function (contact) {
      return {
        view: "by_involves/by_involves",
        startkey: [contact._id, 0], endkey: [contact._id, MAX_TIMESTAMP]
      };
    }, this);
    var query = new GlodaConvQuery();
    query.queryForConversations(constraints, aCallback, aCallbackThis);

    // -- intersect all those conversations
    // -- (fetch the conversation meta-info)
    // -- fetch the messages in the conversations
  }
};
Gloda._init();
