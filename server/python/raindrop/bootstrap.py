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
Setup the CouchDB server so that it is fully usable and what not.
'''
from __future__ import with_statement

import sys
import re
import tempfile
import zipfile
import os, os.path, mimetypes, base64, pprint
import subprocess
import model
import hashlib

import shutil, zipfile
from cStringIO import StringIO
from raindrop import json

from .config import get_config
from .model import get_db
from .model import get_doc_model
from . import proto

import logging
logger = logging.getLogger(__name__)

def path_part_nuke(path, count):
    for i in range(count):
        path = os.path.dirname(path)
    return path
    

LIB_DOC = 'lib' #'_design/files'

# Content-types for file extensions.  This map is preferred over
# mimetypes.guess_type(), and a warning will be issued if we can't get a mime
# type after looking in both.
RAINDROP_CONTENT_TYPES = {
    '.json' : 'application/json',
    '.java' : 'application/java',
    '.psd'  : 'application/octet-stream', # photoshop, srsly?
    '.ttf'  : 'application/x-font',
}

RAINDROP_IGNORE = set(('.patch', '.diff', '.orig', '.zip', '.min' ))

# Updating design documents when not necessary can be expensive as all views
# in that doc are reset. So we've created a helper - a simple 'fingerprinter'
# which creates a dict, each entry holding the finterprint of a single item
# (usually a file), used when files on the file-system are the 'source' of a
# couchdb document. The 'fingerprint' is stored with the document, so later we
# can build the fingerprint of the file-system, and compare them to see if we
# need to update the document. Simple impl - doesn't use the 'stat' of the
# file at all, but always calculates the md5 checksum of the content.
class Fingerprinter:
    def __init__(self):
        self.fs_hashes = {}

    def get_finger(self, filename):
        # it doesn't make sense to calc a file's fingerprint twice
        assert filename not in self.fs_hashes
        ret = self.fs_hashes[filename] = hashlib.md5()
        return ret

    def get_prints(self):
        return dict((n,h.hexdigest()) for (n, h) in self.fs_hashes.iteritems())

# Utility function to extract files from a zip.
# taken from: http://code.activestate.com/recipes/465649/
def extract( filename, dir ):
    zf = zipfile.ZipFile( filename )
    namelist = zf.namelist()
    dirlist = filter( lambda x: x.endswith( '/' ), namelist )
    filelist = filter( lambda x: not x.endswith( '/' ), namelist )
    # make base
    pushd = os.getcwd()
    if not os.path.isdir( dir ):
        os.mkdir( dir )
    os.chdir( dir )
    # create directory structure
    dirlist.sort()
    for dirs in dirlist:
        dirs = dirs.split( '/' )
        prefix = ''
        for dir in dirs:
            dirname = os.path.join( prefix, dir )
            if dir and not os.path.isdir( dirname ):
                os.mkdir( dirname )
            prefix = dirname
    # extract files
    for fn in filelist:
        out = open( fn, 'wb' )
        buffer = StringIO( zf.read( fn ))
        buflen = 2 ** 20
        datum = buffer.read( buflen )
        while datum:
            out.write( datum )
            datum = buffer.read( buflen )
        out.close()
        logger.debug('extracted %r', fn)
    os.chdir( pushd )


def get_client_dir():
    root_dir = path_part_nuke(model.__file__, 4)
    client_dir = os.path.join(root_dir, 'tools', 'clientbuild')
    if os.path.exists(client_dir):
        return client_dir
    else:
        return os.path.join(root_dir, 'client')

def install_client_files(options):
    '''
    cram everyone in 'client' into the app database
    '''
    d = get_db()

    def _insert_file(path, couch_path, attachments, fp):
        minified_path = "%s.min" % path
        
        f = False

        # There is a minified version, and it appears newer
        if os.path.exists(minified_path):
            logger.debug("Found a minified version of %s (%s)" % (path, minified_path))
            if os.path.getmtime(minified_path) > os.path.getmtime(path):
                f = open(minified_path, 'rb')
            else:
                logger.debug("Minified copy is out of date, please update %s from %s" % (minified_path, path))
        
        if not f:           
            f = open(path, 'rb')
        
        ct = RAINDROP_CONTENT_TYPES.get(os.path.splitext(path)[1])
        if not ct:
            ct = mimetypes.guess_type(path)[0]
        if not ct and sys.platform=="win32":
            # A very simplistic check in the windows registry.
            import _winreg
            try:
                k = _winreg.OpenKey(_winreg.HKEY_CLASSES_ROOT,
                                    os.path.splitext(path)[1])
                ct = _winreg.QueryValueEx(k, "Content Type")[0]
            except EnvironmentError:
                pass
        if not ct:
            ct = "application/octet-stream"
            logger.debug("can't guess the content type for '%s' - using %r",
                         path, ct)
        data = f.read()
        fp.get_finger(couch_path).update(data)
        attachments[couch_path] = {
            'content_type': ct,
            'data': base64.b64encode(data)
        }
        f.close()

    def _check_dir(client_dir, couch_path, attachments, fp):
        for filename in os.listdir(client_dir):
            path = os.path.join(client_dir, filename)
            # Insert files if they do not start with a dot or
            # end in a ~, those are probably temp editor files. 
            if os.path.isfile(path) and \
               not filename.startswith(".") and \
               not filename.endswith("~") and \
               not os.path.splitext(path)[1] in RAINDROP_IGNORE:
                _insert_file(path, couch_path + filename, attachments, fp)
            elif os.path.isdir(path):
                new_couch_path = filename + "/"
                if couch_path:
                    new_couch_path = couch_path + new_couch_path
                _check_dir(path, new_couch_path, attachments, fp)
            logger.debug("filename '%s'", filename)

    def _maybe_update_doc(design_doc, doc_name):
        fp = Fingerprinter()
        attachments = design_doc['_attachments'] = {}
        # we cannot go in a zipped egg...
        root_dir = path_part_nuke(model.__file__, 4)
        client_dir = os.path.join(get_client_dir(), doc_name)
        logger.debug("listing contents of '%s' to look for client files", client_dir)

        # recursively go through directories, adding files.
        _check_dir(client_dir, "", attachments, fp)

        new_prints = fp.get_prints()
        if options.force or design_doc.get('fingerprints') != new_prints:
            logger.info("client files in %r are different - updating doc", doc_name)
            design_doc['fingerprints'] = new_prints
            design_doc['_id'] = doc_name
            return d.updateDocuments([design_doc])
        logger.debug("client files are identical - not updating doc")
        return None

    # we cannot go in a zipped egg...
    root_dir = path_part_nuke(model.__file__, 4)
    client_dir = get_client_dir()
    files = os.listdir(client_dir)
    
    # find all the directories in the client dir
    # and create docs with attachments for each dir.
    for f in files:
        fq_child = os.path.join(client_dir, f)
        if os.path.isdir(fq_child):
            dfd = d.openDoc(f)
            if dfd != {}:
                logger.debug(
                    "document '%(_id)s' already exists at revision %(_rev)s",
                    dfd)
            try:
                _maybe_update_doc(dfd, f)
            except:
                logger.exception("update of document '%s' from file '%s' failed", dfd['_id'], f)


def insert_default_docs(options):
    """
    Inserts documents from the couch_docs directory into the couch.
    """
    dm = get_doc_model()

    def items_from_json(filename, data, fingerprinter):
        "Builds raindrop 'schema items' from a json file"

        try:
            src = json.loads(data)
        except ValueError, exc:
            logger.error("Failed to load %r: %s", filename, exc)
            return []

        assert '_id' not in src, src # "we build all that!"
        # a generic json document with a set of schemas etc...
        assert 'schemas' in src, 'no schemas - dunno what to do!'
        try:
            rd_key = src['rd_key']
        except KeyError:
            ext_id = os.path.splitext(os.path.basename(filename))[0]
            rd_key = ['ext', ext_id]
        ret = []

        finger = fingerprinter.get_finger("!".join(rd_key))
        finger.update(data)

        for name, fields in src['schemas'].items():
            for fname, fval in fields.items():
                if isinstance(fval, basestring) and fval.startswith("RDFILE:"):
                    sname = fval[7:].strip()
                    if sname.startswith("*."):
                        # a special case - means use the same base name but
                        # the new ext.
                        path = os.path.splitext(filename)[0] + sname[1:]
                    else:
                        path = os.path.join(os.path.dirname(filename), sname)
                    try:
                        with open(path) as f:
                            fval = f.read()
                            finger.update(fval)
                    except (OSError, IOError):
                        logger.warning("can't open RDFILE: file %r - skipping it",
                                       path)
                    fields[fname] = fval
            sch_item = {
                'rd_key': rd_key,
                'rd_schema_id': name,
                'items': fields,
                'rd_ext_id': 'rd.core',
            }
            ret.append(sch_item);

        # hack our fingerprinter in...
        for sch_item in ret:
            sch_item['items']['fingerprints'] = fingerprinter.get_prints()

        return ret

    def collect_docs(items, dr, file_list):
        """
        Helper function used by os.walk call to recursively collect files.

        It collects normal 'schema items' as used by the rest of the
        back end and as passed to doc_model.emit_schema_items.
        """
        for f in file_list:
            path = os.path.join(dr, f)
            if os.path.isfile(path) and path.endswith(".json"):
                fprinter = Fingerprinter()
                #Open the file and collect the contents.
                try:
                    with open(path) as contents:
                        data = contents.read()
                        sch_items = items_from_json(path, data, fprinter)
                        items.extend(sch_items)
                except (OSError, IOError):
                    logger.warning("can't open file %r - skipping it", path)
                    continue

    # we cannot go in a zipped egg...
    root_dir = path_part_nuke(model.__file__, 4)
    doc_dir = os.path.join(root_dir, 'couch_docs')
    logger.debug("listing contents of '%s' to look for couch docs", doc_dir)

    # load all the .json files, searching recursively in directories
    items = []
    os.path.walk(doc_dir, collect_docs, items)

    #For all the schema items loaded from disk, fetch the docs from
    #the couch, then compare to see if any need to be updated.
    dids = [dm.get_doc_id_for_schema_item(i).encode('utf-8') for i in items]

    result = dm.db.listDoc(keys=dids, include_docs=True)
    updates = []
    for did, item, r in zip(dids, items, result['rows']):
        if 'error' in r or 'deleted' in r['value']:
            # need to create a new item...
            logger.debug("couch doc %r doesn't exist or is deleted - updating",
                         did)
            updates.append(item)
        else:
            fp = item['items']['fingerprints']
            existing = r['doc']
            assert existing['_id']==did
            if not options.force and fp == existing.get('fingerprints'):
                logger.debug("couch doc %r hasn't changed - skipping", did)
            else:
                logger.info("couch doc %r has changed - updating", did)
                item['items']['_id'] = did
                item['items']['_rev'] = existing['_rev']
                updates.append(item)
    if updates:
        dm.create_schema_items(updates)

    #Use the dids to compare with UI extensions, if there is a UI extension
    #that is in the couch, but not on disk, delete it. In the long run,
    #this is hazardous because it may wipe out user-installed extensions,
    #but our more immediate need is to remove cruft as we continue development.
    #All the FE extensions are checked into the trunk at the moment.
    results = dm.open_view(key=["schema_id", "rd.ext.uiext"], include_docs=True, reduce=False)
    all_rows = results['rows']
    deletes = []
    for row in all_rows:
        if not row['doc']['_id'] in dids:
            logger.debug("deleting UI extension %s", row['doc']['rd_key']);
            deletes.append(row['doc'])

    if deletes:
        dm.delete_documents(deletes)

def update_apps():
    """Updates the app config file using the latest app docs in the couch.
       Should be run after a UI app/extension is added or removed from the couch.
    """
    db = get_db()
    dm = get_doc_model()
    replacements = {}

    keys = [
        ["schema_id", "rd.ext.ui"],
        ["schema_id", "rd.ext.uiext"],
    ]
    results = dm.open_view(keys=keys, include_docs=True, reduce=False)
    all_rows = results['rows']

    # Convert couch config value for module paths
    # to a JS string to be used in rdconfig.js
    subs = "subs: ["
    exts = "exts: ["
    paths = []
    module_paths = ""

    # TODO: this needs more work/reformatting
    # but need a real use case first.
    # module_paths += ",".join(
    #    ["'%s': '%s'" % (
    #       item["key"].replace("'", "\\'"), 
    #       item["value"].replace("'", "\\'")
    #    ) for item in view_results["rows"]]
    # )

    # Build up a complete list of required resources.
    for row in all_rows:
        if 'error' in row or 'deleted' in row['value']:
            continue
        doc = row["doc"]
        if 'subscriptions' in doc:
            # sub is an object. Get the keys and
            # add it to the text output.
            for key in doc['subscriptions'].keys():
              subs += "{'%s': '%s'}," % (
                  key.replace("'", "\\'"),
                  doc['subscriptions'][key].replace("'", "\\'")
              )
        if 'modulePaths' in doc:
            for key in doc["modulePaths"].keys():
                paths.append({
                    "key": key,
                    "value": doc["modulePaths"][key]
                })
        
        # skip disabled extensions
        if 'disabled' in doc and doc['disabled'] == True:
            continue;

        try:
            extender = doc["exts"]
        except KeyError:
            continue
        for key in extender.keys():
              exts += "{'%s': '%s'}," % (
                  key.replace("'", "\\'"),
                  extender[key].replace("'", "\\'")
              )

    # join the paths together
    if len(paths) > 0:
        module_paths += ",".join(
           ["'%s': '%s'" % (
              item["key"].replace("'", "\\'"), 
              item["value"].replace("'", "\\'")
           ) for item in paths]
        ) + ","

    # TODO: if my python fu was greater, probably could do this with some
    # fancy list joins, but falling back to removing trailing comma here.
    exts = re.sub(",$", "", exts)
    exts += "]"
    subs = re.sub(",$", "", subs)
    subs += "],"

    doc = db.openDoc(LIB_DOC, attachments=True)

    # Find rdconfig.js skeleton on disk   
    # we cannot go in a zipped egg...
    root_dir = path_part_nuke(model.__file__, 4)
    config_path = os.path.join(root_dir, "client/lib/rdconfig.js")

    # load rdconfig.js skeleton
    f = open(config_path, 'rb')
    data = f.read()
    f.close()

    # Get the hg version we are at
    rev = ''
    if os.path.exists(os.path.join(root_dir, ".hg")):
        # the subprocess call could return line endings we do not want.
        rev = subprocess.Popen(["hg", "id", "-b", "-i", "-t"], stdout=subprocess.PIPE).communicate()[0].replace("\n", "").replace("\r", "")

    # update rdconfig.js contents with couch data
    data = data.replace("/*INSERT REV HERE*/", rev)
    data = data.replace("/*INSERT PATHS HERE*/", module_paths)
    data = data.replace("/*INSERT SUBS HERE*/", subs)
    data = data.replace("/*INSERT EXTS HERE*/", exts)

    new = {
        'content_type': "application/x-javascript; charset=UTF-8",
        'data': base64.b64encode(data)
    }
    # save rdconfig.js in the files.
    if doc["_attachments"]["rdconfig.js"] != new:
        logger.info("rdconfig.js in %r has changed; updating", doc['_id'])
        doc["_attachments"]["rdconfig.js"] = new
        doc['_id'] = LIB_DOC
        db.updateDocuments([doc])


def check_accounts(config=None):
    db = get_db()
    dm = get_doc_model()
    if config is None:
        config = get_config()

    all_idids = set()
    for acct_name, acct_info_all in config.accounts.iteritems():
        acct_id = "account!" + acct_info_all['id']
        logger.debug("Checking account '%s'", acct_id)
        rd_key = ['raindrop-account', acct_id]

        infos = dm.open_schemas([(rd_key, 'rd.account')])
        assert len(infos) == 1
        # We use a 'whitelist' of attributes - these are only written for
        # convenience so queries can determine accounts of a particular
        # protocol.
        attrs = "username", "proto"
        acct_info = {'id': acct_info_all.get('id'),
                     'username': acct_info_all.get('username'),
                     'proto': acct_info_all.get('proto'),
                     }
        # Our account objects know how to turn this config info into the
        # 'identity' list stored with the accounts - so get that.
        try:
            acct = proto.protocols[acct_info['proto']](dm, acct_info_all)
            ids = acct.get_identities()
            for idid in ids:
                all_idids.add(dm.hashable_key(idid))
            # turn tuples back into the lists the couch will return
            acct_info['identities'] = [list(iid) for iid in ids]
        except:
            logger.exception("failed to fetch identities for %r", acct_id)

        new_info = {'rd_key' : rd_key,
                    'rd_schema_id': 'rd.account',
                    'rd_ext_id': 'raindrop.core',
                    'items': acct_info}
        existing = infos[0]
        if existing is not None:
            # See if the items are identical, and skip if they are.
            for name, value in acct_info.iteritems():
                if existing.get(name)!=value:
                    break
            else:
                # they are identical
                logger.debug("account '%(_id)s' is up-to-date at revision %(_rev)s",
                             existing)
                continue
            new_info['_id'] = existing['_id']
            new_info['_rev'] = existing['_rev']
            logger.info("account '%(_id)s' already exists at revision %(_rev)s"
                        " - updating", new_info)
        else:
            logger.info("Adding account '%s'", acct_id)
        dm.create_schema_items([new_info])
    # write a default 'inflow' grouping-tag, which catches messages tagged
    # with each of our identities.
    if all_idids:
        new_info = {'rd_key' : ['display-group', 'inflow'],
                    'rd_schema_id': 'rd.grouping.info',
                    'rd_ext_id': 'raindrop.core',
                    'items': {
                        'title' : "The inflow",
                        'grouping_tags': ['identity-' + '-'.join(idid) for idid in all_idids],
                        }}
        dm.create_schema_items([new_info])
    


# Functions working with design documents holding views.
def install_views(options, include_tests=False):
    db = get_db()
    schema_src = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                              "../../../schema"))

    docs = [d for d in generate_view_docs_from_filesystem(schema_src)]
    if not include_tests:
        docs = [d for d in docs if not d['_id'].endswith("tests")]
    logger.debug("Found %d documents in '%s'", len(docs), schema_src)
    assert docs, 'surely I have *some* docs!'
    # ack - I need to open existing docs first to get the '_rev' property.
    results = []
    for doc in docs:
        try:
            jsonDoc = db.openDoc(doc['_id'])
        except db.NotFoundError:
            jsonDoc = {}
        results.append(jsonDoc)

    put_docs = []
    for existing, doc in zip(results, docs):
        if existing:
            assert existing['_id']==doc['_id']
            assert '_rev' not in doc
            if not options.force and \
                doc['fingerprints'] == existing.get('fingerprints'):
                    logger.debug("design doc %r hasn't changed - skipping",
                                 doc['_id'])
                    continue
            existing.update(doc)
            doc = existing
        logger.info("design doc %r has changed - updating", doc['_id'])
        put_docs.append(doc)
    if put_docs:
        get_db().updateDocuments(put_docs)
        # and update the views immediately...
        get_doc_model()._update_important_views()


def _build_views_doc_from_directory(ddir):
    # all we look for is the views.  And the lists.  And the shows :)
    ret = {}
    fprinter = Fingerprinter()
    ret_views = ret['views'] = {}
    mtail = "-map"
    rtail = "-reduce"
    ltail = "-list"
    stail = "-show"
    rwtail = "-rewrites"
    optstail = "-options"
    files = os.listdir(ddir)
    for fn in files:
        if not fn.endswith(".js"):
            continue
        fqf = os.path.join(ddir, fn)

        tail = mtail + ".js"
        if fn.endswith(tail):
            view_name = fn[:-len(tail)]
            info = ret_views.setdefault(view_name, {})
            try:
                with open(fqf) as f:
                    data = f.read()
                    info['map'] = data
                    fprinter.get_finger(view_name+tail).update(data)
            except (OSError, IOError):
                logger.warning("can't open map file %r - skipping this view", fqf)
                continue

        tail = rtail + ".js"
        if fn.endswith(tail):
            view_name = fn[:-len(tail)]
            info = ret_views.setdefault(view_name, {})
            try:
                with open(fqf) as f:
                    # support the 'builtin' reduce functionality - if line 1
                    # starts with an '_', only that line is used.
                    first = f.readline()
                    if first.startswith('_'):
                        data = first.strip()
                    else:
                        data = first + f.read()
                    info['reduce'] = data
                    fprinter.get_finger(view_name+tail).update(data)
            except (OSError, IOError):
                # no reduce - no problem...
                logger.debug("no reduce file %r - skipping reduce for view '%s'",
                             fqr, view_name)

        tail = ltail + ".js"
        if fn.endswith(tail):
            list_name = fn[:-len(tail)]
            info = ret.setdefault('lists', {})
            try:
                with open(fqf) as f:
                    data = f.read()
                    info[list_name] = data
                    fprinter.get_finger(list_name+tail).update(data)
            except (OSError, IOError):
                logger.warning("can't open list file %r - skipping this list", fqf)
                continue

        tail = rwtail + ".js"
        if fn.endswith(tail):
            rewrite_name = fn[:-len(tail)]
            try:
                with open(fqf) as f:
                    data = f.readline()
                    ret_rewrites = ret.setdefault('rewrites', [])
                    ret_rewrites.extend(eval(data))
                    fprinter.get_finger(rewrite_name+tail).update(data)
            except (OSError, IOError):
                logger.warning("can't open list file %r - skipping this rewrite", fqf)
                continue

        tail = optstail + ".json"
        if fn.endswith(tail):
            view_name = fn[:-len(tail)]
            try:
                with open(fqf) as f:
                    data = f.read()
                    info = ret_views.setdefault(view_name, {})
                    info['options'] = json.loads(data)
                    fprinter.get_finger(view_name+tail).update(data)
            except ValueError, why:
                logger.warning("can't json-decode %r: %s", fqf, why)
                continue
        
    ret['fingerprints'] = fprinter.get_prints()
    ret['language'] = "javascript"
    logger.debug("Document in directory %r has views %s", ddir, ret_views.keys())
    if not ret_views:
        logger.warning("Document in directory %r appears to have no views", ddir)
    return ret


def generate_view_docs_from_filesystem(root):
    # We use the same file-system layout as 'CouchRest' does:
    # http://jchrisa.net/drl/_design/sofa/_show/post/release__couchrest_0_9_0
    # note however that we don't create a design documents in exactly the same
    # way - the view is always named as specified, and currently no 'map only'
    # view is created (and if/when it is, only it will have a "special" name)
    # See http://groups.google.com/group/raindrop-core/web/maintaining-design-docs

    # This is pretty dumb (but therefore simple).
    # root/* -> directories used purely for a 'namespace'
    # root/*/* -> directories which hold the contents of a document.
    # root/*/*-map.js and maybe *-reduce.js -> view content with name b4 '-'
    logger.debug("Starting to build design documents from %r", root)
    for top_name in os.listdir(root):
        fq_child = os.path.join(root, top_name)
        if not os.path.isdir(fq_child):
            logger.debug("skipping non-directory: %s", fq_child)
            continue
        # so we have a 'namespace' directory.
        num_docs = 0
        for doc_name in os.listdir(fq_child):
            fq_doc = os.path.join(fq_child, doc_name)
            if not os.path.isdir(fq_doc):
                logger.info("skipping document non-directory: %s", fq_doc)
                continue
            # have doc - build a dict from its dir.
            doc = _build_views_doc_from_directory(fq_doc)
            # XXX - note the artificial 'raindrop' prefix - the intent here
            # is that we need some way to determine which design documents we
            # own, and which are owned by extensions...
            # XXX - *sob* - and that we shouldn't use '/' in the doc ID at the
            # moment (well - we probably could if we ensured we quoted all the
            # '/' chars, but that seems too much burden for no gain...)
            doc['_id'] = '_design/' + ('!'.join(['raindrop', top_name, doc_name]))
            yield doc
            num_docs += 1

        if not num_docs:
            logger.info("skipping sub-directory without child directories: %s", fq_child)
