#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__ = 'GPL v3'
__copyright__ = '2012, Kovid Goyal <kovid@kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import os
from collections import namedtuple

from calibre import strftime
from calibre.customize import CatalogPlugin
from calibre.customize.conversion import OptionRecommendation, DummyReporter
from calibre.ebooks import calibre_cover
from calibre.library import current_library_name
from calibre.library.catalogs import AuthorSortMismatchException, EmptyCatalogException
from calibre.ptempfile import PersistentTemporaryFile
from calibre.utils.localization import calibre_langcode_to_name, canonicalize_lang, get_lang

Option = namedtuple('Option', 'option, default, dest, action, help')


class MAGIC_MOBI(CatalogPlugin):
    'Magic Mobi catalog generator'

    name = 'Catalog_MAGIC_MOBI'
    description = 'MOBI Magic catalog generator'
    supported_platforms = ['windows', 'osx', 'linux']
    minimum_calibre_version = (0, 7, 40)
    author = 'yosssoy'
    version = (1, 0, 0)
    file_types = set(['mobi'])

    THUMB_SMALLEST = "1.0"
    THUMB_LARGEST = "2.0"

    cli_options = [Option('--catalog-title',  # {{{
                          default='My Books',
                          dest='catalog_title',
                          action=None,
                          help=_('Title of generated catalog used as title in metadata.\n'
                          "Default: '%default'\n"
                          "Applies to: AZW3, MOBI output formats")),
                   Option('--library-url',
                          default='http://192.168.2.1/Calibre Portable/Calibre Library',
                          dest='library_url',
                          action=None,
                          help=_("Specifies Calibre library URL.")),
                   Option('--debug-pipeline',
                           default=None,
                           dest='debug_pipeline',
                           action=None,
                           help=_("Save the output from different stages of the conversion "
                           "pipeline to the specified "
                           "directory. Useful if you are unsure at which stage "
                           "of the conversion process a bug is occurring.\n"
                           "Default: '%default'")),
                   Option('--exclusion-tags',
                          default="['" + _('Catalogs') + "']",
                          dest='exclusion_tags',
                          action=None,
                          help=_("Specifies the tag name used to exclude books from the generated catalog.")),
                   Option('--generate-series',
                          default=True,
                          dest='generate_series',
                          action='store_true',
                          help=_("Include 'Series' section in catalog.\n"
                          "Default: '%default'\n")),
                   Option('--output-profile',
                          default=None,
                          dest='output_profile',
                          action=None,
                          help=_("Specifies the output profile.  In some cases, an output profile is required to optimize the catalog for the device.  For example, 'kindle' or 'kindle_dx' creates a structured Table of Contents with Sections and Articles.\n"
                          "Default: '%default'\n"
                          "Applies to: AZW3, MOBI output formats")),
                   Option('--use-existing-cover',
                          default=False,
                          dest='use_existing_cover',
                          action='store_true',
                          help=_("Replace existing cover when generating the catalog.\n"
                          "Default: '%default'\n"
                          "Applies to: AZW3, MOBI output formats")),
                   Option('--thumb-width',
                          default='1.0',
                          dest='thumb_width',
                          action=None,
                          help=_("Size hint (in inches) for book covers in catalog.\n"
                          "Range: 1.0 - 2.0\n"
                          "Default: '%default'\n"
                          "Applies to AZW3, MOBI output formats")),
                          ]
    # }}}

    def run(self, path_to_output, opts, db, notification=DummyReporter()):
        from calibre_plugins.magic_mobi.generate import CatalogBuilder
        from calibre.utils.logging import default_log as log

        opts.log = log
        opts.fmt = self.fmt = 'mobi'

        # Add local options
        #opts.creator = '%s, %s %s, %s' % (strftime('%A'), strftime('%B'), strftime('%d').lstrip('0'), strftime('%Y'))
        opts.creator = '%s %s' % ('calibre', strftime('%Y-%m-%d'))
        opts.creator_sort_as = '%s %s' % ('calibre', strftime('%Y-%m-%d'))
        opts.connected_kindle = False

        # Finalize output_profile
        op = opts.output_profile
        if op is None:
            op = 'default'

        if opts.connected_device['name'] and 'kindle' in opts.connected_device['name'].lower():
            opts.connected_kindle = True
            op = "kindle"
            if opts.connected_device['serial']:
                sno = opts.connected_device['serial'][:4]
                if sno in ['B004', 'B005']:
                    op = "kindle_dx"
                elif sno in ['B024', 'B01B', 'B01C', 'B01D', 'B01F']:
                    op = "kindle_pw"

        opts.description_clip = 380 if (op.endswith('dx') or op.endswith('pw')) or 'kindle' not in op else 100
        opts.author_clip = 100 if (op.endswith('dx') or op.endswith('pw')) or 'kindle' not in op else 60
        opts.output_profile = op

        opts.basename = "Catalog"
        opts.cli_environment = not hasattr(opts, 'sync')

        build_log = []

        build_log.append(u"%s('%s'): Generating %s %sin %s environment, locale: '%s'" %
            (self.name,
             current_library_name(),
             self.fmt,
             'for %s ' % opts.output_profile if opts.output_profile else '',
             'CLI' if opts.cli_environment else 'GUI',
             calibre_langcode_to_name(canonicalize_lang(get_lang()), localize=False))
             )

        if opts.connected_device['is_device_connected'] and \
           opts.connected_device['kind'] == 'device':
            if opts.connected_device['serial']:
                build_log.append(u" connected_device: '%s' #%s%s " % \
                    (opts.connected_device['name'],
                     opts.connected_device['serial'][0:4],
                     'x' * (len(opts.connected_device['serial']) - 4)))
                for storage in opts.connected_device['storage']:
                    if storage:
                        build_log.append(u"  mount point: %s" % storage)
            else:
                build_log.append(u" connected_device: '%s'" % opts.connected_device['name'])
                try:
                    for storage in opts.connected_device['storage']:
                        if storage:
                            build_log.append(u"  mount point: %s" % storage)
                except:
                    build_log.append(u"  (no mount points)")
        else:
            build_log.append(u" connected_device: '%s'" % opts.connected_device['name'])

        opts_dict = vars(opts)
        if opts_dict['ids']:
            build_log.append(" book count: %d" % len(opts_dict['ids']))

        sections_list = []
        sections_list.append('Authors')
        sections_list.append('Titles')
        if opts.generate_series:
            sections_list.append('Series')
        sections_list.append('Genres')
        sections_list.append('Descriptions')

        if not sections_list:
            if opts.cli_environment:
                opts.log.warn('*** No Section switches specified, enabling all Sections ***')
                opts.generate_series = True
                sections_list = ['Authors', 'Titles', 'Series', 'Genres', 'Descriptions']
            else:
                opts.log.warn('\n*** No enabled Sections, terminating catalog generation ***')
                return ["No Included Sections", "No enabled Sections.\nCheck E-book options tab\n'Included sections'\n"]

        opts.log(u" Sections: %s" % ', '.join(sections_list))
        opts.section_list = sections_list

        # Limit thumb_width to 1.0" - 2.0"
        try:
            if float(opts.thumb_width) < float(self.THUMB_SMALLEST):
                log.warning("coercing thumb_width from '%s' to '%s'" % (opts.thumb_width, self.THUMB_SMALLEST))
                opts.thumb_width = self.THUMB_SMALLEST
            if float(opts.thumb_width) > float(self.THUMB_LARGEST):
                log.warning("coercing thumb_width from '%s' to '%s'" % (opts.thumb_width, self.THUMB_LARGEST))
                opts.thumb_width = self.THUMB_LARGEST
            opts.thumb_width = "%.2f" % float(opts.thumb_width)
        except:
            log.error("coercing thumb_width from '%s' to '%s'" % (opts.thumb_width, self.THUMB_SMALLEST))
            opts.thumb_width = "1.0"

        # eval exclusion_rules if passed from command line
        if type(opts.exclusion_tags) is not list:
            log.info(type(opts.exclusion_tags))
            try:
                opts.exclusion_tags = eval(opts.exclusion_tags)
            except:
                log.error("malformed --exclusion-tags: %s" % opts.exclusion_tags)
                raise

        # Display opts
        keys = opts_dict.keys()
        keys.sort()
        build_log.append(" opts:")
        for key in keys:
            if key in ['catalog_title', 'author_clip', 'connected_kindle', 'creator',
                       'description_clip', 
                       'exclusion_tags', 'fmt',
                       'output_profile',
                       'search_text', 'sort_by', 'sync',
                       'thumb_width', 'use_existing_cover', 'wishlist_tag']:
                build_log.append("  %s: %s" % (key, repr(opts_dict[key])))
        if opts.verbose:
            log('\n'.join(line for line in build_log))
        self.opts = opts

        # Launch the Catalog builder
        catalog = CatalogBuilder(db, opts, self, report_progress=notification)

        if opts.verbose:
            log.info(" Begin catalog source generation")

        try:
            catalog.build_sources()
            if opts.verbose:
                log.info(" Completed catalog source generation\n")
        except (AuthorSortMismatchException, EmptyCatalogException), e:
            log.error(" *** Terminated catalog generation: %s ***" % e)
        except:
            log.error(" unhandled exception in catalog generator")
            raise

        else:
            recommendations = []
            recommendations.append(('remove_fake_margins', False,
                OptionRecommendation.HIGH))
            recommendations.append(('comments', '', OptionRecommendation.HIGH))

            """
            >>> Use to debug generated catalog code before pipeline conversion <<<
            """
            GENERATE_DEBUG_EPUB = False
            if GENERATE_DEBUG_EPUB:
                catalog_debug_path = os.path.join(os.path.expanduser('~'), 'Desktop', 'Catalog debug')
                setattr(opts, 'debug_pipeline', os.path.expanduser(catalog_debug_path))

            dp = getattr(opts, 'debug_pipeline', None)
            if dp is not None:
                recommendations.append(('debug_pipeline', dp,
                    OptionRecommendation.HIGH))

            if opts.output_profile and opts.output_profile.startswith("kindle"):
                recommendations.append(('output_profile', opts.output_profile,
                    OptionRecommendation.HIGH))
                recommendations.append(('book_producer', opts.output_profile,
                    OptionRecommendation.HIGH))
                if opts.fmt == 'mobi':
                    recommendations.append(('no_inline_toc', True,
                        OptionRecommendation.HIGH))
                    recommendations.append(('verbose', 2,
                        OptionRecommendation.HIGH))

            # Use existing cover or generate new cover
            cpath = None
            existing_cover = False
            try:
                search_text = 'title:"%s" author:%s' % (
                        opts.catalog_title.replace('"', '\\"'), 'calibre')
                matches = db.search(search_text, return_matches=True)
                if matches:
                    cpath = db.cover(matches[0], index_is_id=True, as_path=True)
                    if cpath and os.path.exists(cpath):
                        existing_cover = True
            except:
                pass

            if self.opts.use_existing_cover and not existing_cover:
                log.warning("no existing catalog cover found")

            if self.opts.use_existing_cover and existing_cover:
                recommendations.append(('cover', cpath, OptionRecommendation.HIGH))
                log.info("using existing catalog cover")
            else:
                log.info("replacing catalog cover")
                new_cover_path = PersistentTemporaryFile(suffix='.jpg')
                new_cover = calibre_cover(opts.catalog_title.replace('"', '\\"'), 'calibre')
                new_cover_path.write(new_cover)
                new_cover_path.close()
                recommendations.append(('cover', new_cover_path.name, OptionRecommendation.HIGH))

            # Run ebook-convert
            from calibre.ebooks.conversion.plumber import Plumber
            plumber = Plumber(os.path.join(catalog.catalog_path, opts.basename + '.opf'),
                            path_to_output, log, report_progress=notification,
                            abort_after_input_dump=False)
            plumber.merge_ui_recommendations(recommendations)
            plumber.run()

            try:
                os.remove(cpath)
            except:
                pass

            if GENERATE_DEBUG_EPUB:
                from calibre.ebooks.epub import initialize_container
                from calibre.ebooks.tweak import zip_rebuilder
                from calibre.utils.zipfile import ZipFile
                input_path = os.path.join(catalog_debug_path, 'input')
                epub_shell = os.path.join(catalog_debug_path, 'epub_shell.zip')
                initialize_container(epub_shell, opf_name='content.opf')
                with ZipFile(epub_shell, 'r') as zf:
                    zf.extractall(path=input_path)
                os.remove(epub_shell)
                zip_rebuilder(input_path, os.path.join(catalog_debug_path, 'input.epub'))

        # returns to gui2.actions.catalog:catalog_generated()
        return catalog.error
