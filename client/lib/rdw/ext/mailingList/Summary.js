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

/*jslint nomen: false, plusplus: false */
/*global dojo: false, rdw: false, rd: false */
"use strict";

dojo.provide("rdw.ext.mailingList.Summary");

dojo.require("rdw._Base");
dojo.require("rdw.ext.mailingList.model");

rd.addStyle("rdw.ext.mailingList.Summary");

dojo.declare("rdw.ext.mailingList.Summary", [rdw._Base], {
    // The ID of the mailing list.  This must be passed to the constructor
    // so postCreate can use it to retrieve the document from the datastore.
    listId: null,
  
    templatePath: dojo.moduleUrl("rdw.ext.mailingList", "Summary.html"),
  
    postCreate: function () {
        //summary: dijit lifecycle method after template insertion in the DOM.
        this.inherited("postCreate", arguments);
        rdw.ext.mailingList.model.register(this.listId, this);
    },
    
    destroy: function () {
        //summary: dijit lifecycle method, when destroying the dijit.
        rdw.ext.mailingList.model.unregister(this.listId, this);
        this.inherited("destroy", arguments);
    },

    onMailingListSummaryUpdate: function (doc) {
        //ID is required
        rd.escapeHtml(doc.id, this.idNode, "only");

        //Name is not a required field so we check for it
        if (doc.name) {
            rd.escapeHtml(doc.name, this.nameNode, "only");
        }
    }
});
