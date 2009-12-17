/* ***** BEGIN LICENSE BLOCK *****
 * Version: MPL 1.1
 *
 * The contents of this file are subject to the Mozilla Public License Version
 * 1.1 (the "License"); you may not use this file except in compliance with
 * the License. You may obtain a copy of the License at
 * http://www.mozilla.org/MPL/
 *
 * Software distributed under the License is distributed on an "AS IS" basis,
 * WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License
 * for the specific language governing rights and limitations under the
 * License.
 *
 * The Original Code is Raindrop.
 *
 * The Initial Developer of the Original Code is
 * Mozilla Messaging, Inc..
 * Portions created by the Initial Developer are Copyright (C) 2009
 * the Initial Developer. All Rights Reserved.
 *
 * Contributor(s):
 * */

dojo.provide("rdw.ext.mailingList.SummaryGroup");

dojo.require("rd.api");
dojo.require("rdw._Base");
dojo.require("rdw.ext.mailingList.model");

rd.addStyle("rdw.ext.mailingList.SummaryGroup");

dojo.declare("rdw.ext.mailingList.SummaryGroup", [rdw._Base], {
  // The ID of the mailing list.  This must be passed to the constructor
  // so postCreate can use it to retrieve the document from the datastore.
  listId: null,

  templateString: dojo.cache("rdw.ext.mailingList", "SummaryGroup.html"),

  postCreate: function() {
    //summary: dijit lifecycle method after template insertion in the DOM.
    this.inherited("postCreate", arguments);
    rdw.ext.mailingList.model.register(this.listId, this);
  },
  
  destroy: function() {
    //summary: dijit lifecycle method, when destroying the dijit.
    rdw.ext.mailingList.model.unregister(this.listId, this);
    this.inherited("destroy", arguments);
  },

  onMailingListSummaryUpdate: function(doc) {
    this.doc = doc;
    dojo.attr(this.subscriptionStatusNode, "status", doc.status);
    dojo.attr(this.subscriptionActionNode, "status", doc.status);

    rd.escapeHtml(doc.identity[1], this.identityNode, "only");

    //Archive is not a required field, check for it
    if (doc.archive && doc.archive.http) {
        this.archiveHttpNode.href = doc.archive.http;
    }

    //Post is not a required field and is often only an email
    if (doc.post) {
      if (doc.post.http)
        this.postHttpNode.href = doc.post.http;
      else
        this.postHttpNode.style.display = "none";

      if (doc.post.mailto)
        this.postEmailNode.href = doc.post.mailto;
      else
        this.postEmailNode.style.display = "none";
    }

    //Help is not a required field, check for it
    if (doc.help) {
      if (doc.help.http)
        this.helpHttpNode.href = doc.help.http;
      else
        this.helpHttpNode.style.display = "none";

      if (doc.help.mailto)
        this.helpEmailNode.href = doc.help.mailto;
      else
        this.helpEmailNode.style.display = "none";
    }

    // TODO: make this localizable.
    switch(doc.status) {
      case "subscribed":
        rd.escapeHtml("Subscribed", this.subscriptionStatusNode, "only");
        rd.escapeHtml("Unsubscribe", this.subscriptionActionNode, "only");
        break;
      case "unsubscribe-pending":
      case "unsubscribe-confirmed":
        rd.escapeHtml("Unsubscribe Pending", this.subscriptionStatusNode, "only");
        //XXX Future
        //rd.escapeHtml("Cancel Unsubscribe", this.subscriptionActionNode, "only");
        break;
      case "unsubscribed":
        rd.escapeHtml("Unsubscribed", this.subscriptionStatusNode, "only");
        //XXX Future
        //rd.escapeHtml("Re-Subscribe", this.subscriptionActionNode, "only");
        break;
    }
  },

  /**
   * Unsubscribe from a mailing list.
   *
   * This method uses the parsed List-Unsubscribe headers of email messages
   * from lists to determine how to issue the unsubscription request. The
   * Mailing List headers should have been properly parsed in the mailing list
   * extension.  Any errors found here should likely be logged with support
   * tickets so we can update the extension for better parsing.
   *
   */
  onSubscription: function() {
    // Don't do anything unless the user is subscribed to the list.
    if (this.doc.status != "subscribed")
      return;

    // TODO: do all this in the mailing list extractor extension so we know
    // whether or not we understand how to unsubscribe from this mailing list
    // right from the start and can enable/disable the UI accordingly.
    // TODO: If we can't unsubscribe the user, explain it to them nicely.
    if (!this.doc.unsubscribe && !this.doc.unsubscribe.mailto)
      throw "can't unsubscribe from mailing list; no unsubscribe info";

    if (!confirm("Are you sure you want to unsubscribe from " + this.doc.id + "?  " +
                 "You won't receive messages from the mailing list anymore, " +
                 "and if you resubscribe later you won't receive the messages " +
                 "that were sent to the list while you were unsubscribed."))
      return;

    // TODO: retrieve the list from the store again and make sure its status
    // is still "subscribed" and we're still able to unsubscribe from it.

    this.doc.status = "unsubscribe-pending";
    rdw.ext.mailingList.model.put(this.doc)
    .ok(this, function(doc) {
      this._unsubscribe(this.doc.unsubscribe.mailto);
    })
    .error(this, function(error) {
      // TODO: update the UI to notify the user about the problem.
      //alert("error updating list: " + error);
    });
  },

  _unsubscribe: function(/*String*/spec) {
    var url = new dojo._Url(spec);
    //alert("scheme: " + url.scheme + "; authority: " + url.authority +
    //      "; path: " + url.path +   "; query: " + url.query +
    //      "; fragment " + url.fragment);

    // url.path == the email address
    // url.query == can contain subject and/or body parameters

    var params = url.query ? dojo.queryToObject(url.query) : {};

    rd.api().createSchemaItem({
      //TODO: make a better rd_key.
      rd_key: ["manually_created_doc", (new Date()).getTime()],
      rd_schema_id: "rd.msg.outgoing.simple",
      items: {
        from: this.doc.identity,
        // TODO: use the user's name in the from_display.
        from_display: this.doc.identity[1],
        to: [["email", url.path]],
        to_display: [url.path],
        // Hopefully the mailto: URL has provided us with the necessary subject
        // and body.  If not, we guess "subscribe" for both subject and body.
        // TODO: make better guesses based on knowledge of the mailing list
        // software being used to run the mailing list.
        subject: params.subject ? params.subject : "unsubscribe",
        body: params.body ? params.body : "unsubscribe",
        outgoing_state: "outgoing"
      }
    })
    .ok(this, function(message) {
      //alert("unsubscribe request sent");
    })
    .error(this, function(error) {
      alert("error sending unsubscribe request: " + error);
      // TODO: set the list's status back to "subscribed".
    });
  }
});
