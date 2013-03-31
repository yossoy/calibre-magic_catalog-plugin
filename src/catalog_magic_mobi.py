#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import with_statement

__license__   = 'GPL v3'
__copyright__ = '2013, yosssoy <yossoy@gmail.com>'
__docformat__ = 'restructuredtext en'

import re, sys

from calibre.ebooks.conversion.config import load_defaults
from calibre.gui2 import gprefs, open_url
from calibre.utils.icu import sort_key

from calibre_plugins.magic_mobi.catalog_magic_mobi_ui import Ui_Form
from PyQt4.Qt import (Qt, QWidget, QUrl)
from urllib import quote
from urlparse import urljoin, urlparse, urlunparse
from calibre.ebooks.oeb.base import urlnormalize

def parse_library_url(url):
    r = urlnormalize(url)
    if not r.endswith('/'):
        r += '/'
    return r

class PluginWidget(QWidget,Ui_Form):

    TITLE = _('E-book options')
    HELP  = _('Options specific to')+' MOBI '+_('output')
    DEBUG = False

    # Output synced to the connected device?
    sync_enabled = True

    # Formats supported by this plugin
    formats = set(['mobi'])

    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.setupUi(self)

    def initialize(self, name):
        self.name = name
        opt_value = gprefs.get(self.name + '_' + 'library_url', 'http://192.168.2.1/Calibre/Calibre Library')
        self.library_url.setText(opt_value if opt_value else '')
        self.library_url.textChanged.connect(self.library_url_changed)
        self.library_url_changed()
        opt_value = gprefs.get(self.name + '_' + 'exclusion_tags', u'[' + _('Catalog') + u']')
        try:
            opt_value = eval(opt_value)
            opt_value = ', '.join(opt_value)
        except:
            opt_value = None
        self.excluded_tags.setText(opt_value if opt_value else '')

    def library_url_changed(self):
        r = parse_library_url(unicode(self.library_url.text()))
        self.url_result.clear()
        if r:
            self.url_result.setText(r + '<<book folder>>')
        else:
            self.url_result.setText(_('*** Invalid URL ***'))

    def options(self):
        # Save/return the current options
        # exclude_genre stores literally
        # Section switches store as True/False
        # others store as lists

        opts_dict = {}
        opt_value = unicode(self.library_url.text())
        opts_dict['library_url'] = opt_value
        gprefs.set(self.name + '_' + 'library_url', opt_value)
        opt_value = unicode(self.excluded_tags.text())
        opt_value = unicode([tag.strip() for tag in opt_value.split(',')])
        gprefs.set(self.name + '_' + 'exclusion_tags', opt_value)
        opts_dict['exclusion_tags'] = opt_value

        opts_dict['generate_series'] = True
        opts_dict['generate_recently_added'] = False
        try:
            opts_dict['output_profile'] = [load_defaults('page_setup')['output_profile']]
        except:
            opts_dict['output_profile'] = ['default']
        opts_dict['use_existing_cover'] = False
        opts_dict['thumb_width'] = 1.0

        if self.DEBUG:
            print "opts_dict"
            for opt in sorted(opts_dict.keys(), key=sort_key):
                print " %s: %s" % (opt, repr(opts_dict[opt]))
        return opts_dict

    def show_help(self):
        '''
        Display help file
        '''
        open_url(QUrl('http://manual.calibre-ebook.com/catalogs.html'))

