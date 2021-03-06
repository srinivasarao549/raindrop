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
/*global require: false, clearTimeout: false, setTimeout: false */
"use strict";

require.def("rdw/placeholder",
["require", "rd", "dojo"],
function (require, rd, dojo) {

    /**
     * Set the input value to use placeholder value if HTML5 placeholder
     * attribute is not supported.
     * @param {DOMNode} input an input element.
     */
    function setPlaceholder(input) {
        //If no native support for placeholder then JS to the rescue!
        var missingNative = !("placeholder" in input),
            placeholder = input.getAttribute("placeholder"),
            trimmed = dojo.trim(input.value);

        if (!trimmed || trimmed === placeholder) {
            dojo.addClass(input, "placeholder");
            if (missingNative) {
                input.value = placeholder;
                if (placeholder === "password" && input.type === "password") {
                    input.type = "text";
                }
            }
        } else {
            dojo.removeClass(input, "placeholder");
        }
    }

    /**
     * Handles focus events on the node to see if placehoder needs to be removed.
     * @param {Event} evt
     */
    function onfocus(evt) {
        //Clear out placeholder, change the style.
        var input = evt.target,
            placeholder = input.getAttribute("placeholder");
        if (input.value === placeholder) {
            if (!("placeholder" in input)) {
                input.value = "";
                if (placeholder === "password" && input.type === "text") {
                    input.type = "password";
                }
            }
            dojo.removeClass(input, "placeholder");
        }
    }

    /** Handles blur events on the node to see if placeholder needs to be reinstated.
     * @param {Event} evt
     */
    function onblur(evt) {
        //Reset placeholder text if necessary.
        setPlaceholder(evt.target);
    }

    /**
     * Scans domNode and its children for text input/textarea elements that have a placeholder
     * attribute, and attach placeholder behavior to it.
     * Allow for the existence of browsers that already have placeholder support
     * built in.
     * 
     * @param {DOMNode} domNode
     * @param {Widget} [widget] an optional widget that will track the connect handles.
     *
     * @returns an array of connect handles that can be used for dojo.disconnect calls.
     */
    return function (domNode, widget) {
        var handles = [];
        dojo.query('input[type="text"], input[type="password"], textarea', domNode).forEach(function (node) {
            //Skip nodes that have already been bound
            if (node.getAttribute("data-rdwPlaceholder") !== "set") {
                var obj = widget || dojo;
                handles.push(obj.connect(node, "onfocus", onfocus));
                handles.push(obj.connect(node, "onblur", onblur));

                node.setAttribute("data-rdwPlaceholder", "set");
            }

            //Set up initial state.
            setPlaceholder(node);
        });

        return handles;
    };
});
