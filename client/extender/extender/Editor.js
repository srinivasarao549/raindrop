dojo.provide("extender.Editor");

dojo.require("dijit.form.Textarea");
dojo.require("dojox.io.xhrMultiPart");
dojo.require("rdw._Base");
dojo.require("couch");

//Uses script-added styles to allow loading on demand at the cost of a
//custom build that would load all styles at the beginning.
rd.addStyle("extender.css.Editor");

dojo.declare("extender.Editor", [rdw._Base], {

  //Holds the module name that is being edited.
  moduleName: "",

  templatePath: dojo.moduleUrl("extender.templates", "Editor.html"),
  widgetsInTemplate: true,

  postCreate: function() {
    //summary: dijit lifecycle method, after template is in the DOM.

    //Some hackery to get .js path from Dojo code.
    //TODO: does not work with xdomain loaded modules.
    var parts = this.moduleName.split(".");
    this.path = dojo.moduleUrl(parts.slice(0, -1).join("."), parts[parts.length - 1] + ".js").toString();
    var text = dojo._getText(this.path + "?nocache=" + ((new Date()).getTime()));

    this.textArea.attr("value", text);
  },

  onSave: function(evt) {
    //summary: handles click events to save button.
    
    //Find out what couch doc to modify by taking out the
    //module name from the path.
    var modulePath = this.moduleName.replace(/\./g, "/");
    var index = this.path.indexOf(modulePath);
    var couchDocPath = this.path.substring(0, index - 1);

    //Load the couch doc so we can get the _rev field.
    dojo.xhrGet({
      url: couchDocPath,
      handleAs: "json",
      handle: dojo.hitch(this, function(response, ioArgs) {
        if (response instanceof Error) {
          this.updateStatus("Error: " + response);
        } else {
          var _rev = response._rev;

          dojo.xhrPut({
            url: couchDocPath + "/" + modulePath + ".js?rev=" + _rev,
            headers: {
              "Content-Type": "application/javascript"
            },
            putData: this.textArea.attr("value"),
            handle: dojo.hitch(this, function(response, ioArgs) {
              if (response instanceof Error) {
                this.updateStatus("Error: " + response);
              } else {
                this.updateStatus("File Saved");
              }
            })            
          });
        }
      })
    })
  },

  updateStatus: function(message) {
    //summary: updates the status node with a message using an
    //animation.
    dojo.style(this.statusNode, "opacity", 0);
    this.statusNode.innerHTML = rd.escapeHtml(message);
    dojo.anim(
      this.statusNode, {
        opacity: 1
      },
      500,
      null,
      dojo.hitch(this, function() {
        setTimeout(dojo.hitch(this, function() {
          dojo.style(this.statusNode, "opacity", 0);
        }), 5000);
      })
    );
  }
});