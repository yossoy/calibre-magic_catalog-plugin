# -*- coding: utf-8 -*-

__license__ = 'GPL v3'
__copyright__ = '2010, Greg Riker'

import datetime, htmlentitydefs, os, platform, re, shutil, unicodedata, zlib
from copy import deepcopy
from xml.sax.saxutils import escape
from calibre import config_dir

from calibre import (prepare_string_for_xml, strftime, force_unicode,
        isbytestring)
from calibre.constants import isosx, cache_dir
from calibre.customize.conversion import DummyReporter
from calibre.customize.ui import output_profiles
from calibre.ebooks.BeautifulSoup import BeautifulSoup, BeautifulStoneSoup, Tag, NavigableString
from calibre.ebooks.chardet import substitute_entites
from calibre.ebooks.metadata import author_to_author_sort
from calibre.library.catalogs import AuthorSortMismatchException, EmptyCatalogException, \
                                     InvalidGenresSourceFieldException
from calibre.ptempfile import PersistentTemporaryDirectory
from calibre.utils.date import format_date, is_date_undefined, now as nowf
from calibre.utils.filenames import ascii_text, shorten_components_to
from calibre.utils.icu import capitalize, collation_order, sort_key
from calibre.utils.magick.draw import thumbnail
from calibre.utils.zipfile import ZipFile
from calibre.utils.localization import langnames_to_langcodes, get_language, get_lang, lang_as_iso639_1
from urllib import pathname2url, quote
from templite import Templite
from calibre_plugins.magic_mobi.catalog_magic_mobi import parse_library_url
from urlparse import urljoin
class CatalogBuilder(object):
    '''
    Generates catalog source files from calibre database

    Flow of control:
        gui2.actions.catalog:generate_catalog()
        gui2.tools:generate_catalog() or library.cli:command_catalog()
        called from gui2.convert.gui_conversion:gui_catalog()
        catalog = Catalog(notification=Reporter())
        catalog.build_sources()
    Options managed in gui2.catalog.catalog_magic_mobi.py

    Does not work with AZW3, interferes with new prefix handling
    '''

    DEBUG = False

    # A single number creates 'Last x days' only.
    # Multiple numbers create 'Last x days', 'x to y days ago' ...
    # e.g, [7,15,30,60] or [30]
    # [] = No date ranges added
    DATE_RANGE = [30]

    # Text used in generated catalog for title section with other-than-ASCII leading letter
    SYMBOLS = _('Symbols')

    # basename              output file basename
    # creator               dc:creator in OPF metadata
    # description_clip       limits size of NCX descriptions (Kindle only)
    # includeSources        Used in filter_excluded_genres to skip tags like '[SPL]'
    # notification          Used to check for cancel, report progress
    # stylesheet            CSS stylesheet
    # title                 dc:title in OPF metadata, NCX periodical
    # verbosity             level of diagnostic printout

    ''' device-specific symbol (default empty star) '''
    @property
    def SYMBOL_EMPTY_RATING(self):
        return self.output_profile.empty_ratings_char

    ''' device-specific symbol (default filled star) '''
    @property
    def SYMBOL_FULL_RATING(self):
        return self.output_profile.ratings_char

    ''' device-specific symbol for reading progress '''
    @property
    def SYMBOL_PROGRESS_READ(self):
        psr = '&#9642;'
        return psr

    ''' device-specific symbol for reading progress '''
    @property
    def SYMBOL_PROGRESS_UNREAD(self):
        psu = '&#9643;'
        return psu

    ''' device-specific symbol for reading progress '''
    @property
    def SYMBOL_READING(self):
        return '&#x25b7;'

    def __init__(self, db, _opts, plugin,
                    report_progress=DummyReporter(),
                    stylesheet="content/magic_stylesheet.css",
                    init_resources=True):

        self.db = db
        self.opts = _opts

        self.library_base_path = db.library_path

        self.plugin = plugin
        self.reporter = report_progress
        self.stylesheet = stylesheet
        self.cache_dir = os.path.join(cache_dir(), 'catalog')
        self.catalog_path = PersistentTemporaryDirectory("_magic_mobi_catalog", prefix='')
        self.content_dir = os.path.join(self.catalog_path, "content")
        self.excluded_tags = self.get_excluded_tags()

        self.all_series = set()
        self.authors = None
        self.books_by_author = None
        self.books_by_date_range = None
        self.books_by_description = []
        self.books_by_month = None
        self.books_by_series = None
        self.books_by_title = None
        self.books_by_title_no_series_prefix = None
        self.books_to_catalog = None
        self.current_step = 0.0
        self.error = []
        self.genres = []
        self.genre_tags_dict = self.filter_genre_tags(max_len=245 - len("%s/Genre_.html" % self.content_dir)) # @@@
        self.html_filelist_1 = []
        self.html_filelist_2 = []
        self.individual_authors = None
        self.ncx_soup = None
        self.output_profile = self.get_output_profile(_opts)
        self.play_order = 1
        self.progress_int = 0.0
        self.progress_string = ''
        self.thumb_height = 0
        self.thumb_width = 0
        self.thumbs = None
        self.thumbs_path = os.path.join(self.cache_dir, "thumbs.zip")
        self.total_steps = 6.0
        self.use_series_prefix_in_titles_section = False

        self.dump_custom_fields()
        self.books_to_catalog = self.fetch_books_to_catalog()
        self.compute_total_steps()
        self.calculate_thumbnail_dimensions()
        self.confirm_thumbs_archive()
        if init_resources:
            self.copy_catalog_resources()

    """ key() functions """

    def _kf_author_to_author_sort(self, author):
        """ Compute author_sort value from author

        Tokenize author string, return capitalized string with last token first

        Args:
         author (str): author, e.g. 'John Smith'

        Return:
         (str): 'Smith, john'
        """
        tokens = author.split()
        tokens = tokens[-1:] + tokens[:-1]
        if len(tokens) > 1:
            tokens[0] += ','
        return ' '.join(tokens).capitalize()

    def _kf_books_by_author_sorter_author(self, book):
        """ Generate book sort key with computed author_sort.

        Generate a sort key of computed author_sort, title. Used to look for
        author_sort mismatches.
        Twiddle included to force series to sort after non-series books.
         'Smith, john Star Wars'
         'Smith, john ~Star Wars 0001.0000'

        Args:
         book (dict): book metadata

        Return:
         (str): sort key
        """
        if not book['series']:
            key = '%s %s' % (self._kf_author_to_author_sort(book['author']),
                                capitalize(book['title_sort']))
        else:
            index = book['series_index']
            integer = int(index)
            fraction = index - integer
            series_index = '%04d%s' % (integer, str('%0.4f' % fraction).lstrip('0'))
            key = '%s ~%s %s' % (self._kf_author_to_author_sort(book['author']),
                                    self.generate_sort_title(book['series']),
                                    series_index)
        return key

    def _kf_books_by_author_sorter_author_sort(self, book, longest_author_sort=60):
        """ Generate book sort key with supplied author_sort.

        Generate a sort key of author_sort, title.
        Bang, tilde included to force series to sort after non-series books.

        Args:
         book (dict): book metadata

        Return:
         (str): sort key
        """
        if not book['series']:
            fs = u'{:<%d}!{!s}' % longest_author_sort
            key = fs.format(capitalize(book['author_sort']),
                            capitalize(book['title_sort']))
        else:
            index = book['series_index']
            integer = int(index)
            fraction = index - integer
            series_index = u'%04d%s' % (integer, str(u'%0.4f' % fraction).lstrip(u'0'))
            fs = u'{:<%d}~{!s}{!s}' % longest_author_sort
            key = fs.format(capitalize(book['author_sort']),
                            self.generate_sort_title(book['series']),
                            series_index)
        return key

    def _kf_books_by_series_sorter(self, book):
        index = book['series_index']
        integer = int(index)
        fraction = index - integer
        series_index = '%04d%s' % (integer, str('%0.4f' % fraction).lstrip('0'))
        key = '%s %s' % (self.generate_sort_title(book['series']),
                         series_index)
        return key

    """ Methods """

    def build_sources(self):
        """ Generate catalog source files.

        Assemble OPF, HTML and NCX files reflecting catalog options.
        Generated source is OEB compliant.
        Called from gui2.convert.gui_conversion:gui_catalog()

        Args:

        Exceptions:
            AuthorSortMismatchException
            EmptyCatalogException

        Results:
         error: problems reported during build

        """

        self.fetch_books_by_title()
        self.fetch_books_by_author()
        self.generate_thumbnails()
        self.generate_html_descriptions()
        self.generate_html_by_author()
        self.generate_html_by_series()
        self.generate_html_by_genres()
        self.generate_opf()
        self.generate_ncx_header()
        self.generate_ncx_by_author(_("Authors"))
        self.generate_ncx_descriptions(_("Descriptions"))
        if self.opts.generate_series:
            self.generate_ncx_by_series(_("Series"))
        self.generate_ncx_by_genre(_("Genres"))
        self.write_ncx()

    def calculate_thumbnail_dimensions(self):
        """ Calculate thumb dimensions based on device DPI.

        Using the specified output profile, calculate thumb_width
        in pixels, then set height to width * 1.33. Special-case for
        Kindle/MOBI, as rendering off by 2.

        Inputs:
         opts.thumb_width (str|float): specified thumb_width
         opts.output_profile.dpi (int): device DPI

        Outputs:
         thumb_width (float): calculated thumb_width
         thumb_height (float): calculated thumb_height
        """

        for x in output_profiles():
            if x.short_name == self.opts.output_profile:
                # aspect ratio: 3:4
                self.thumb_width = x.dpi * float(self.opts.thumb_width)
                self.thumb_height = self.thumb_width * 1.33
                break
        if self.opts.verbose:
            self.opts.log(" Thumbnails:")
            self.opts.log("  DPI = %d; thumbnail dimensions: %d x %d" % \
                            (x.dpi, self.thumb_width, self.thumb_height))

    def compute_total_steps(self):
        """ Calculate number of build steps to generate catalog.

        Calculate total number of build steps based on enabled sections.

        Inputs:
         opts.generate_* (bool): enabled sections

        Outputs:
         total_steps (int): updated
        """
        # Tweak build steps based on optional sections:  1 call for HTML, 1 for NCX
        incremental_jobs = 0
        incremental_jobs += 2
        if self.opts.generate_series:
            incremental_jobs += 2
        # +1 thumbs
        self.total_steps += incremental_jobs

    def confirm_thumbs_archive(self):
        """ Validate thumbs archive.

        Confirm existence of thumbs archive, or create if absent.
        Confirm stored thumb_width matches current opts.thumb_width,
        or invalidate archive.
        generate_thumbnails() writes current thumb_width to archive.

        Inputs:
         opts.thumb_width (float): requested thumb_width
         thumbs_path (file): existing thumbs archive

        Outputs:
         thumbs_path (file): new (non_existent or invalidated), or
                                  validated existing thumbs archive
        """
        if not os.path.exists(self.cache_dir):
            self.opts.log.info("  creating new thumb cache '%s'" % self.cache_dir)
            os.makedirs(self.cache_dir)
        if not os.path.exists(self.thumbs_path):
            self.opts.log.info('  creating thumbnail archive, thumb_width: %1.2f"' %
                               float(self.opts.thumb_width))
            with ZipFile(self.thumbs_path, mode='w') as zfw:
                zfw.writestr("Catalog Thumbs Archive", '')
        else:
            try:
                with ZipFile(self.thumbs_path, mode='r') as zfr:
                    try:
                        cached_thumb_width = zfr.read('thumb_width')
                    except:
                        cached_thumb_width = "-1"
            except:
                os.remove(self.thumbs_path)
                cached_thumb_width = '-1'

            if float(cached_thumb_width) != float(self.opts.thumb_width):
                self.opts.log.warning("  invalidating cache at '%s'" % self.thumbs_path)
                self.opts.log.warning('  thumb_width changed: %1.2f" => %1.2f"' %
                                      (float(cached_thumb_width), float(self.opts.thumb_width)))
                with ZipFile(self.thumbs_path, mode='w') as zfw:
                    zfw.writestr("Catalog Thumbs Archive", '')
            else:
                self.opts.log.info('  existing thumb cache at %s, cached_thumb_width: %1.2f"' %
                                   (self.thumbs_path, float(cached_thumb_width)))

    def convert_html_entities(self, s):
        """ Convert string containing HTML entities to its unicode equivalent.

        Convert a string containing HTML entities of the form '&amp;' or '&97;'
        to a normalized unicode string. E.g., 'AT&amp;T' converted to 'AT&T'.

        Args:
         s (str): str containing one or more HTML entities.

        Return:
         s (str): converted string
        """
        matches = re.findall("&#\d+;", s)
        if len(matches) > 0:
            hits = set(matches)
            for hit in hits:
                name = hit[2:-1]
                try:
                    entnum = int(name)
                    s = s.replace(hit, unichr(entnum))
                except ValueError:
                    pass

        matches = re.findall("&\w+;", s)
        hits = set(matches)
        amp = "&amp;"
        if amp in hits:
            hits.remove(amp)
        for hit in hits:
            name = hit[1:-1]
            if htmlentitydefs.name2codepoint in name:
                    s = s.replace(hit, unichr(htmlentitydefs.name2codepoint[name]))
        s = s.replace(amp, "&")
        return s

    def copy_catalog_resources(self):
        """ Copy resources from calibre source to self.catalog_path.

        Copy basic resources - default cover, stylesheet, and masthead (Kindle only)
        from calibre resource directory to self.catalog_path, a temporary directory
        for constructing the catalog. Files stored to specified destination dirs.

        Inputs:
         files_to_copy (files): resource files from calibre resources, which may be overridden locally

        Output:
         resource files copied to self.catalog_path/*
        """
        self.create_catalog_directory_structure()

        user_path = os.path.join(config_dir, 'resources/magic_catalog')

        files_to_copy = [('', 'DefaultCover.jpg'),
                         ('content', 'magic_stylesheet.css')]
        files_to_copy.extend([('images', 'mastheadImage.gif')])

        files = ['magic_catalog/' + file[1] for file in files_to_copy]
        arcfiles = self.plugin.load_resources(files)

        self.opts.log.info("create catalog directory (userpath=\"%s\"" % user_path)
        for file in files_to_copy:
            srcpath = os.path.join(user_path, file[1])
            dstpath = self.catalog_path if file[0] == '' else os.path.join(self.catalog_path, file[0])
            if (os.path.exists(srcpath)):
                self.opts.log.info(" - use user file \"%s\"" % srcpath)
                shutil.copy(srcpath, dstpath)
            else:
                if not os.path.isdir(dstpath):
                    os.makedirs(dstpath)
                self.opts.log.info(" - use arc file \"%s\"" % file[1])
                with open(os.path.join(dstpath, file[1]), 'wb') as f:
                    f.write(arcfiles['magic_catalog/' + file[1]])
        try:
            self.generate_masthead_image(os.path.join(self.catalog_path, 'images/mastheadImage.gif'))
        except:
            pass

    def create_catalog_directory_structure(self):
        """ Create subdirs in catalog output dir.

        Create /content and /images in self.catalog_path

        Inputs:
         catalog_path (path): path to catalog output dir

        Output:
         /content, /images created
        """
        if not os.path.isdir(self.catalog_path):
            os.makedirs(self.catalog_path)

        content_path = self.catalog_path + "/content"
        if not os.path.isdir(content_path):
            os.makedirs(content_path)
        images_path = self.catalog_path + "/images"
        if not os.path.isdir(images_path):
            os.makedirs(images_path)

    def detect_author_sort_mismatches(self, books_to_test):
        """ Detect author_sort mismatches.

        Sort by author, look for inconsistencies in author_sort among
        similarly-named authors. Fatal for MOBI generation, a mere
        annoyance for EPUB.

        Inputs:
         books_by_author (list): list of books to test, possibly unsorted

        Output:
         (none)

        Exceptions:
         AuthorSortMismatchException: author_sort mismatch detected
        """

        books_by_author = sorted(list(books_to_test), key=self._kf_books_by_author_sorter_author)

        authors = [(record['author'], record['author_sort']) for record in books_by_author]
        current_author = authors[0]
        for (i, author) in enumerate(authors):
            if author != current_author and i:
                if author[0] == current_author[0]:
                    if self.opts.fmt == 'mobi':
                        # Exit if building MOBI
                        error_msg = _("<p>Inconsistent Author Sort values for Author<br/>" +
                                      "'{!s}':</p>".format(author[0]) +
                                      "<p><center><b>{!s}</b> != <b>{!s}</b></center></p>".format(author[1], current_author[1]) +
                                      "<p>Unable to build MOBI catalog.<br/>" +
                                      "Select all books by '{!s}', apply correct Author Sort value in Edit Metadata dialog, then rebuild the catalog.\n<p>".format(author[0]))

                        self.opts.log.warn('\n*** Metadata error ***')
                        self.opts.log.warn(error_msg)

                        self.error.append('Author Sort mismatch')
                        self.error.append(error_msg)
                        raise AuthorSortMismatchException, "author_sort mismatch while building MOBI"
                    else:
                        # Warning if building non-MOBI
                        if not self.error:
                            self.error.append('Author Sort mismatch')

                        error_msg = _("Warning: Inconsistent Author Sort values for Author '{!s}':\n".format(author[0]) +
                                      " {!s} != {!s}\n".format(author[1], current_author[1]))
                        self.opts.log.warn('\n*** Metadata warning ***')
                        self.opts.log.warn(error_msg)
                        self.error.append(error_msg)
                        continue

                current_author = author

    def dump_custom_fields(self):
        """
        Dump custom field mappings for debugging
        """
        if self.opts.verbose:
            self.opts.log.info(" Custom fields:")
            all_custom_fields = self.db.custom_field_keys()
            for cf in all_custom_fields:
                self.opts.log.info("  %-20s %-20s %s" %
                    (cf, "'%s'" % self.db.metadata_for_field(cf)['name'],
                     self.db.metadata_for_field(cf)['datatype']))

    def establish_equivalencies(self, item_list, key=None):
        """ Return icu equivalent sort letter.

        Returns base sort letter for accented characters. Code provided by
        chaley, modified to force unaccented base letters for A, O & U when
        an accented version would otherwise be returned.

        Args:
         item_list (list): list of items, sorted by icu_sort

        Return:
         cl_list (list): list of equivalent leading chars, 1:1 correspondence to item_list
        """

        # Hack to force the cataloged leading letter to be
        # an unadorned character if the accented version sorts before the unaccented
        exceptions = {
                        u'Ä':   u'A',
                        u'Ö':   u'O',
                        u'Ü':   u'U'
                     }

        if key is not None:
            sort_field = key

        cl_list = [None] * len(item_list)
        last_ordnum = 0

        for idx, item in enumerate(item_list):
            if key:
                c = item[sort_field]
            else:
                c = item

            ordnum, ordlen = collation_order(c)
            if isosx and platform.mac_ver()[0] < '10.8':
                # Hackhackhackhackhack
                # icu returns bogus results with curly apostrophes, maybe others under OS X 10.6.x
                # When we see the magic combo of 0/-1 for ordnum/ordlen, special case the logic
                last_c = u''
                if ordnum == 0 and ordlen == -1:
                    if icu_upper(c[0]) != last_c:
                        last_c = icu_upper(c[0])
                        if last_c in exceptions.keys():
                            last_c = exceptions[unicode(last_c)]
                        last_ordnum = ordnum
                    cl_list[idx] = last_c
                else:
                    if last_ordnum != ordnum:
                        last_c = icu_upper(c[0:ordlen])
                        if last_c in exceptions.keys():
                            last_c = exceptions[unicode(last_c)]
                        last_ordnum = ordnum
                    cl_list[idx] = last_c

            else:
                if last_ordnum != ordnum:
                    last_c = icu_upper(c[0:ordlen])
                    if last_c in exceptions.keys():
                        last_c = exceptions[unicode(last_c)]
                    last_ordnum = ordnum
                cl_list[idx] = last_c

        if self.DEBUG and self.opts.verbose:
            print("     establish_equivalencies():")
            if key:
                for idx, item in enumerate(item_list):
                    print("      %s %s" % (cl_list[idx], item[sort_field]))
            else:
                    print("      %s %s" % (cl_list[idx], item))

        return cl_list

    def fetch_books_by_author(self):
        """ Generate a list of books sorted by author.

        For books with multiple authors, relist book with additional authors.
        Sort the database by author. Report author_sort inconsistencies as warning when
        building EPUB or MOBI, error when building MOBI. Collect a list of unique authors
        to self.authors.

        Inputs:
         self.books_to_catalog (list): database, sorted by title

        Outputs:
         books_by_author: database, sorted by author
         authors: list of book authors. Two credited authors are considered an
          individual entity
         error: author_sort mismatches

        Return:
         True: no errors
         False: author_sort mismatch detected while building MOBI
        """

        self.update_progress_full_step(_("Sorting database"))

        books_by_author = list(self.books_to_catalog)
        self.detect_author_sort_mismatches(books_by_author)

        # Assumes books_by_title already populated
        # init books_by_description before relisting multiple authors
        books_by_description = list(books_by_author)
        books_by_author = self.relist_multiple_authors(books_by_author)

        # Determine the longest author_sort length before sorting
        asl = [i['author_sort'] for i in books_by_author]
        las = max(asl, key=len)

        self.books_by_description = sorted(books_by_description,
                                           key=lambda x: sort_key(self._kf_books_by_author_sorter_author_sort(x, len(las))))

        books_by_author = sorted(books_by_author,
            key=lambda x: sort_key(self._kf_books_by_author_sorter_author_sort(x, len(las))))

        if self.DEBUG and self.opts.verbose:
            tl = [i['title'] for i in books_by_author]
            lt = max(tl, key=len)
            fs = '{:<6}{:<%d} {:<%d} {!s}' % (len(lt), len(las))
            print(fs.format('', 'Title', 'Author', 'Series'))
            for i in books_by_author:
                print(fs.format('', i['title'], i['author_sort'], i['series']))

        # Build the unique_authors set from existing data
        authors = [(record['author'], capitalize(record['author_sort'])) for record in books_by_author]

        # authors[] contains a list of all book authors, with multiple entries for multiple books by author
        #        authors[]: (([0]:friendly  [1]:sort))
        # unique_authors[]: (([0]:friendly  [1]:sort  [2]:book_count))
        books_by_current_author = 0
        current_author = authors[0]
        multiple_authors = False
        unique_authors = []
        individual_authors = set()
        for (i, author) in enumerate(authors):
            if author != current_author:
                # Note that current_author and author are tuples: (friendly, sort)
                multiple_authors = True

                # New author, save the previous author/sort/count
                unique_authors.append((current_author[0], icu_title(current_author[1]),
                                        books_by_current_author))
                current_author = author
                books_by_current_author = 1
            elif i == 0 and len(authors) == 1:
                # Allow for single-book lists
                unique_authors.append((current_author[0], icu_title(current_author[1]),
                                        books_by_current_author))
            else:
                books_by_current_author += 1
        else:
            # Add final author to list or single-author dataset
            if (current_author == author and len(authors) > 1) or not multiple_authors:
                unique_authors.append((current_author[0], icu_title(current_author[1]),
                                        books_by_current_author))

        self.authors = list(unique_authors)
        self.books_by_author = books_by_author

        for ua in unique_authors:
            for ia in ua[0].replace(' &amp; ', ' & ').split(' & '):
                individual_authors.add(ia)
        self.individual_authors = list(individual_authors)

        if self.DEBUG and self.opts.verbose:
            self.opts.log.info("\nfetch_books_by_author(): %d unique authors" % len(unique_authors))
            for author in unique_authors:
                self.opts.log.info((u" %-50s %-25s %2d" % (author[0][0:45], author[1][0:20],
                    author[2])).encode('utf-8'))
            self.opts.log.info("\nfetch_books_by_author(): %d individual authors" % len(individual_authors))
            for author in sorted(individual_authors):
                self.opts.log.info("%s" % author)

        return True

    def fetch_books_by_title(self):
        """ Generate a list of books sorted by title.

        Sort the database by title.

        Inputs:
         self.books_to_catalog (list): database

        Outputs:
         books_by_title: database, sorted by title

        Return:
         True: no errors
         False: author_sort mismatch detected while building MOBI
        """
        self.update_progress_full_step(_("Sorting titles"))
        # Re-sort based on title_sort
        if len(self.books_to_catalog):
            self.books_by_title = sorted(self.books_to_catalog, key=lambda x: sort_key(x['title_sort'].upper()))

            if self.DEBUG and self.opts.verbose:
                self.opts.log.info("fetch_books_by_title(): %d books" % len(self.books_by_title))
                self.opts.log.info(" %-40s %-40s" % ('title', 'title_sort'))
                for title in self.books_by_title:
                    self.opts.log.info((u" %-40s %-40s" % (title['title'][0:40],
                                                            title['title_sort'][0:40])).encode('utf-8'))
        else:
            error_msg = _("No books to catalog.\nCheck 'Excluded books' rules in E-book options.\n")
            self.opts.log.error('*** ' + error_msg + ' ***')
            self.error.append(_('No books available to include in catalog'))
            self.error.append(error_msg)
            raise EmptyCatalogException, error_msg

    def fetch_books_to_catalog(self):
        """ Populate self.books_to_catalog from database

        Create self.books_to_catalog from filtered database.
        Keys:
         authors            massaged
         author_sort        record['author_sort'] or computed
         cover              massaged record['cover']
         date               massaged record['pubdate']
         description        massaged record['comments']
         id                 record['id']
         formats            massaged record['formats']
         publisher          massaged record['publisher']
         rating             record['rating'] (0 if None)
         series             record['series'] or None
         series_index       record['series_index'] or 0.0
         short_description  truncated description
         tags               filtered record['tags']
         timestamp          record['timestamp']
         title              massaged record['title']
         title_sort         computed from record['title']
         uuid               record['uuid']

        Inputs:
         data (list): filtered list of book metadata dicts

        Outputs:
         (list) books_to_catalog

        Returns:
         True: Successful
         False: Empty data, (check filter restrictions)
        """

        def _populate_title(record):
            ''' populate this_title with massaged metadata '''
            this_title = {}

            this_title['id'] = record['id']
            this_title['uuid'] = record['uuid']

            this_title['title'] = self.convert_html_entities(record['title'])
            if record['series']:
                this_title['series'] = record['series']
                self.all_series.add(this_title['series'])
                this_title['series_index'] = record['series_index']
            else:
                this_title['series'] = None
                this_title['series_index'] = 0.0

            this_title['title_sort'] = self.generate_sort_title(this_title['title'])

            if 'authors' in record:
                this_title['authors'] = record['authors']
                # Synthesize author attribution from authors list
                if record['authors']:
                    this_title['author'] = " &amp; ".join(record['authors'])
                else:
                    this_title['author'] = _('Unknown')
                    this_title['authors'] = [this_title['author']]

            if 'author_sort' in record and record['author_sort'].strip():
                this_title['author_sort'] = record['author_sort']
            else:
                this_title['author_sort'] = self._kf_author_to_author_sort(this_title['author'])

            if record['publisher']:
                this_title['publisher'] = re.sub('&', '&amp;', record['publisher'])

            this_title['rating'] = record['rating'] if record['rating'] else 0

            if is_date_undefined(record['pubdate']):
                this_title['date'] = None
            else:
                this_title['date'] = strftime(u'%B %Y', record['pubdate'].timetuple())

            this_title['timestamp'] = record['timestamp']

            if record['comments']:
                # Strip annotations
                a_offset = record['comments'].find('<div class="user_annotations">')
                ad_offset = record['comments'].find('<hr class="annotations_divider" />')
                if a_offset >= 0:
                    record['comments'] = record['comments'][:a_offset]
                if ad_offset >= 0:
                    record['comments'] = record['comments'][:ad_offset]

                this_title['description'] = self.massage_comments(record['comments'])

                # Create short description
                paras = BeautifulSoup(this_title['description']).findAll('p')
                tokens = []
                for p in paras:
                    for token in p.contents:
                        if token.string is not None:
                            tokens.append(token.string)
                this_title['short_description'] = self.generate_short_description(' '.join(tokens), dest="description")
            else:
                this_title['description'] = None
                this_title['short_description'] = None

            # Merge with custom field/value

            if record['cover']:
                this_title['cover'] = re.sub('&amp;', '&', record['cover'])

            this_title['tags'] = []
            if record['tags']:
                this_title['tags'] = map(self.convert_html_entities, record['tags'])
            this_title['genres'] = this_title['tags']

            this_title['languages'] = "en"
            if record['languages']:
                this_title['languages'] = record['languages']

            if record['formats']:
                formats = []
                for format in record['formats']:
                    formats.append(self.convert_html_entities(format))
                this_title['formats'] = formats

            return this_title

        # Entry point

        self.opts.sort_by = 'title'
        search_phrase = ''
        if self.excluded_tags:
            search_terms = []
            for tag in self.excluded_tags:
                search_terms.append("tag:=%s" % tag)
            search_phrase = "not (%s)" % " or ".join(search_terms)

        # If a list of ids are provided, don't use search_text
        if self.opts.ids:
            self.opts.search_text = search_phrase
        else:
            if self.opts.search_text:
                self.opts.search_text += " " + search_phrase
            else:
                self.opts.search_text = search_phrase

        # Fetch the database as a dictionary
        data = self.plugin.search_sort_db(self.db, self.opts)

        # Populate this_title{} from data[{},{}]
        titles = []
        for record in data:
            this_title = _populate_title(record)
            titles.append(this_title)
        return titles

    def filter_genre_tags(self, max_len):
        """ Remove excluded tags from data set, return normalized genre list.

        Filter all db tags, removing excluded tags supplied in opts.
        Test for multiple tags resolving to same normalized form. Normalized
        tags are flattened to alphanumeric ascii_text.

        Args:
         max_len: maximum length of normalized tag to fit within OS constraints

        Return:
         genre_tags_dict (dict): dict of filtered, normalized tags in data set
        """

        def _format_tag_list(tags, indent=1, line_break=70, header='Tag list'):
            def _next_tag(sorted_tags):
                for (i, tag) in enumerate(sorted_tags):
                    if i < len(tags) - 1:
                        yield tag + ", "
                    else:
                        yield tag

            ans = '%s%d %s:\n' % (' ' * indent, len(tags), header)
            ans += ' ' * (indent + 1)
            out_str = ''
            sorted_tags = sorted(tags, key=sort_key)
            for tag in _next_tag(sorted_tags):
                out_str += tag
                if len(out_str) >= line_break:
                    ans += out_str + '\n'
                    out_str = ' ' * (indent + 1)
            return ans + out_str

        def _normalize_tag(tag, max_len):
            """ Generate an XHTML-legal anchor string from tag.

            Parse tag for non-ascii, convert to unicode name.

            Args:
             tags (str): tag name possible containing symbols
             max_len (int): maximum length of tag

            Return:
             normalized (str): unicode names substituted for non-ascii chars,
              clipped to max_len
            """

            normalized = massaged = re.sub('\s', '', ascii_text(tag).lower())
            if re.search('\W', normalized):
                normalized = ''
                for c in massaged:
                    if re.search('\W', c):
                        normalized += self.generate_unicode_name(c)
                    else:
                        normalized += c
            shortened = shorten_components_to(max_len, [normalized])[0]
            return shortened

        # Entry point
        normalized_tags = []
        friendly_tags = []
        excluded_tags = []

        # Fetch all possible genres from source field
        all_genre_tags = self.db.all_tags()
        all_genre_tags.sort()

        for tag in all_genre_tags:
            if tag in self.excluded_tags:
                excluded_tags.append(tag)
                continue

            if tag == ' ':
                continue

            normalized_tags.append(_normalize_tag(tag, max_len))
            friendly_tags.append(tag)

        genre_tags_dict = dict(zip(friendly_tags, normalized_tags))

        # Test for multiple genres resolving to same normalized form
        normalized_set = set(normalized_tags)
        for normalized in normalized_set:
            if normalized_tags.count(normalized) > 1:
                self.opts.log.warn("      Warning: multiple tags resolving to genre '%s':" % normalized)
                for key in genre_tags_dict:
                    if genre_tags_dict[key] == normalized:
                        self.opts.log.warn("       %s" % key)
        if self.opts.verbose:
            self.opts.log.info('%s' % _format_tag_list(genre_tags_dict, header="enabled genres"))
            self.opts.log.info('%s' % _format_tag_list(excluded_tags, header="excluded genres"))

        print("genre_tags_dict: %s" % genre_tags_dict)
        return genre_tags_dict

    def format_ncx_text(self, description, dest=None):
        """ Massage NCX text for Kindle.

        Convert HTML entities for proper display on Kindle, convert
        '&amp;' to '&#38;' (Kindle fails).

        Args:
         description (str): string, possibly with HTM entities
         dest (kwarg): author, title or description

        Return:
         (str): massaged, possibly truncated description
        """
        # Kindle TOC descriptions won't render certain characters
        # Fix up
        massaged = unicode(BeautifulStoneSoup(description, convertEntities=BeautifulStoneSoup.HTML_ENTITIES))

        # Replace '&' with '&#38;'
        massaged = re.sub("&", "&#38;", massaged)

        if massaged.strip() and dest:
            #print traceback.print_stack(limit=3)
            return self.generate_short_description(massaged.strip(), dest=dest)
        else:
            return None

    def generate_author_anchor(self, author):
        """ Generate legal XHTML anchor.

        Convert author to legal XHTML (may contain unicode chars), stripping
        non-alphanumeric chars.

        Args:
         author (str): author name

        Return:
         (str): asciized version of author
        """
        return re.sub("\W", "", ascii_text(author))

    def generate_by_authors_list(self, books):
        authors = []
        current_author = ''
        series = None
        author = None
        author_series_map = {}
        for idx, book in enumerate(books):
            if book['author'] != current_author:
                # start a new author
                author = {}
                current_author = book['author']
                current_series = None
                non_series_books = 0
                author['id'] = "%s" % self.generate_author_anchor(current_author)
                author['name'] = current_author
                author['series'] = []
                if (not author_series_map.has_key(current_author)):
                    author_series_map[current_author] = {}
                author['book_count'] = 0
                authors.append(author)
            current_series = book['series']
            if current_series:
                if author_series_map[current_author].has_key(current_series):
                    series = author_series_map[current_author][current_series]
                else:
                    series = {}
                    author['series'].append(series)
                    author_series_map[current_author][current_series] = series
                    series['name'] = current_series
                    series['url'] = "%s.html#%s" % ('BySeries', self.generate_series_anchor(current_series))
            else:
                series = None
            author['book_count'] += 1

            # Add books
            this_book = {}
            this_book['title'] = book['title']
            this_book['series'] = book['series']
            series_index = str(book['series_index'])
            if series_index.endswith('.0'):
                series_index = series_index[:-2]
            this_book['series_idx'] = series_index
            this_book['pubyear'] = book['date'].split()[1] if book['date'] else None
            this_book['url'] = "book_%d.html" % (int (float(book['id'])))

            if series:
                if not series.has_key('books'):
                    series['books'] = []
                series['books'].append(this_book)
            else:
                if not author.has_key('books'):
                    author['books'] = []
                author['books'].append(this_book)
        return authors

    def generate_html_by_author(self):
        """ Generate content/ByAuthor.html.

        Loop through self.books_by_author, generate HTML
        with anchors for author and index letters.

        Input:
         books_by_author (list): books, sorted by author

        Output:
         content/ByAuthor.html (file)
        """

        friendly_name = _("Authors")
        self.update_progress_full_step("%s HTML" % friendly_name)

        authors = self.generate_by_authors_list(self.books_by_author)

        # Languages
        languages = lang_as_iso639_1(get_lang())

        template = self.load_userfile_or_pluginfile('magic_catalog/magic_author_template.xhtml').decode('utf-8')
        from calibre.ebooks.oeb.base import XHTML_NS
        args = dict(
            authors=authors,
            title_str=friendly_name,
            xmlns=XHTML_NS,
            languages=languages,
            )
        for k, v in args.iteritems():
            if isbytestring(v):
                args[k] = v.decode('utf-8')
        generated_html = Templite(template).render(**args)
        generated_html = substitute_entites(generated_html)

        soup = BeautifulStoneSoup(generated_html)

        outfile_spec = "%s/ByAuthor.html" % (self.content_dir)
        outfile = open(outfile_spec, 'w')
        outfile.write(soup.prettify())
        outfile.close()
        self.html_filelist_1.append("content/ByAuthor.html")

    def generate_html_by_genres(self):
        """ Generate individual HTML files per tag.

        Filter out excluded tags. For each tag qualifying as a genre,
        create a separate HTML file. Normalize tags to flatten synonymous tags.

        Inputs:
         self.genre_tags_dict (list): all genre tags

        Output:
         (files): HTML file per genre
        """

        self.update_progress_full_step(_("Genres HTML"))

        # Extract books matching filtered_tags
        genre_list = []
        for friendly_tag in sorted(self.genre_tags_dict, key=sort_key):
            #print("\ngenerate_html_by_genres(): looking for books with friendly_tag '%s'" % friendly_tag)
            # tag_list => { normalized_genre_tag : [{book},{},{}],
            #               normalized_genre_tag : [{book},{},{}] }

            tag_list = {}
            for book in self.books_by_author:
                # Scan each book for tag matching friendly_tag
                if 'genres' in book and friendly_tag in book['genres']:
                    this_book = {}
                    this_book['author'] = book['author']
                    this_book['title'] = book['title']
                    this_book['author_sort'] = capitalize(book['author_sort'])
                    this_book['tags'] = book['tags']
                    this_book['id'] = book['id']
                    this_book['series'] = book['series']
                    this_book['series_index'] = book['series_index']
                    this_book['date'] = book['date']
                    normalized_tag = self.genre_tags_dict[friendly_tag]
                    genre_tag_list = [key for genre in genre_list for key in genre]
                    if normalized_tag in genre_tag_list:
                        for existing_genre in genre_list:
                            for key in existing_genre:
                                new_book = None
                                if key == normalized_tag:
                                    for book in existing_genre[key]:
                                        if book['title'] == this_book['title']:
                                            new_book = False
                                            break
                                    else:
                                        new_book = True
                                if new_book:
                                    existing_genre[key].append(this_book)
                    else:
                        tag_list[normalized_tag] = [this_book]
                        genre_list.append(tag_list)

        if self.opts.verbose:
            if len(genre_list):
                self.opts.log.info("  Genre summary: %d active genre tags used in generating catalog with %d titles" %
                                (len(genre_list), len(self.books_to_catalog)))

                for genre in genre_list:
                    for key in genre:
                        self.opts.log.info("   %s: %d %s" % (self.get_friendly_genre_tag(key),
                                            len(genre[key]),
                                            'titles' if len(genre[key]) > 1 else 'title'))

        # Write the results
        # genre_list = [ {friendly_tag:[{book},{book}]}, {friendly_tag:[{book},{book}]}, ...]
        master_genre_list = []
        for genre_tag_set in genre_list:
            for (index, genre) in enumerate(genre_tag_set):
                #print "genre: %s  \t  genre_tag_set[genre]: %s" % (genre, genre_tag_set[genre])

                # Create sorted_authors[0] = friendly, [1] = author_sort for NCX creation
                authors = []
                for book in genre_tag_set[genre]:
                    authors.append((book['author'], book['author_sort']))

                # authors[] contains a list of all book authors, with multiple entries for multiple books by author
                # Create unique_authors with a count of books per author as the third tuple element
                books_by_current_author = 1
                current_author = authors[0]
                unique_authors = []
                for (i, author) in enumerate(authors):
                    if author != current_author and i:
                        unique_authors.append((current_author[0], current_author[1], books_by_current_author))
                        current_author = author
                        books_by_current_author = 1
                    elif i == 0 and len(authors) == 1:
                        # Allow for single-book lists
                        unique_authors.append((current_author[0], current_author[1], books_by_current_author))
                    else:
                        books_by_current_author += 1

                # Write the genre book list as an article
                outfile = "%s/Genre_%s.html" % (self.content_dir, genre)
                titles_spanned = self.generate_html_by_genre(genre,
                                                             True if index == 0 else False,
                                                             genre_tag_set[genre],
                                                             outfile)

                tag_file = "content/Genre_%s.html" % genre
                master_genre_list.append({
                                            'tag': genre,
                                            'file': tag_file,
                                            'authors': unique_authors,
                                            'books': genre_tag_set[genre],
                                            'titles_spanned': titles_spanned})

        self.genres = master_genre_list

    def generate_html_by_genre(self, genre, section_head, books, outfile):
        """ Generate individual genre HTML file.

        Generate an individual genre HTML file. Called from generate_html_by_genres()

        Args:
         genre (str): genre name
         section_head (bool): True if starting section
         books (dict): list of books in genre
         outfile (str): full pathname to output file

        Results:
         (file): Genre HTML file written

        Returns:
         titles_spanned (list): [(first_author, first_book), (last_author, last_book)]
        """

        authors = self.generate_by_authors_list(books)

        # Languages
        languages = lang_as_iso639_1(get_lang())
        friendly_name = escape(self.get_friendly_genre_tag(genre))

        template = self.load_userfile_or_pluginfile('magic_catalog/magic_author_template.xhtml').decode('utf-8')
        from calibre.ebooks.oeb.base import XHTML_NS
        args = dict(
            authors=authors,
            title_str=friendly_name,
            xmlns=XHTML_NS,
            languages=languages,
            )
        for k, v in args.iteritems():
            if isbytestring(v):
                args[k] = v.decode('utf-8')
        generated_html = Templite(template).render(**args)
        generated_html = substitute_entites(generated_html)

        soup = BeautifulStoneSoup(generated_html)

        # Write the generated file to content_dir
        outfile = open(outfile, 'w')
        outfile.write(soup.prettify())
        outfile.close()

        if len(books) > 1:
            titles_spanned = [(books[0]['author'], books[0]['title']), (books[-1]['author'], books[-1]['title'])]
        else:
            titles_spanned = [(books[0]['author'], books[0]['title'])]

        return titles_spanned

    def generate_by_series_list(self, books):
        serieses = []
        current_series = None
        series = None
        for idx, book in enumerate(books):
            if book['series'] != current_series:
                series = {}
                current_series = book['series']
                series['name'] = current_series
                series['id'] = self.generate_series_anchor(current_series)
                series['books'] = []
                serieses.append(series)
            this_book = {}
            this_book['author'] = book['author']
            this_book['author_url'] = "ByAuthor.html#%s" % self.generate_author_anchor(book['author'])
            this_book['title'] = book['title']
            series_index = str(book['series_index'])
            if series_index.endswith('.0'):
                series_index = series_index[:-2]
            this_book['series_index'] = series_index
            this_book['pubyear'] = book['date'].split()[1] if book['date'] else None
            this_book['url'] = "book_%d.html" % (int (float(book['id'])))
            series['books'].append(this_book)
        return serieses

    def generate_html_by_series(self):
        """ Generate content/BySeries.html.

        Search database for books in series.

        Input:
         database

        Output:
         content/BySeries.html (file)

        """
        friendly_name = _("Series")
        self.update_progress_full_step("%s HTML" % friendly_name)

        self.opts.sort_by = 'series'

        # *** Convert the existing database, resort by series/index ***
        self.books_by_series = [i for i in self.books_to_catalog if i['series']]
        self.books_by_series = sorted(self.books_by_series, key=lambda x: sort_key(self._kf_books_by_series_sorter(x)))

        if not self.books_by_series:
            self.opts.generate_series = False
            self.opts.log("  no series found in selected books, skipping Series section")
            return

        serieses = self.generate_by_series_list(self.books_by_series)

        # Languages
        languages = lang_as_iso639_1(get_lang())

        template = self.load_userfile_or_pluginfile('magic_catalog/magic_series_template.xhtml').decode('utf-8')
        from calibre.ebooks.oeb.base import XHTML_NS
        args = dict(
            serieses=serieses,
            title_str=friendly_name,
            xmlns=XHTML_NS,
            languages=languages,
            )
        for k, v in args.iteritems():
            if isbytestring(v):
                args[k] = v.decode('utf-8')
        generated_html = Templite(template).render(**args)
        generated_html = substitute_entites(generated_html)
        soup = BeautifulStoneSoup(generated_html)

        outfile_spec = "%s/BySeries.html" % (self.content_dir)

        outfile = open(outfile_spec, 'w')
        outfile.write(soup.prettify())
        outfile.close()
        self.html_filelist_1.append("content/BySeries.html")

    def generate_html_description_header(self, book):
        """ Generate the HTML Description header from template.

        Create HTML Description from book metadata and template.
        Called by generate_html_descriptions()

        Args:
         book (dict): book metadata

        Return:
         soup (BeautifulSoup): HTML Description for book
        """

        from calibre.ebooks.oeb.base import XHTML_NS

        def _generate_html():
            args = dict(
			book_id=book_id,
                        author=author,
                        author_url=author_url,
                        comments=comments,
                        css=css,
                        formats=formats,
                        genres=genres,
                        note_content=note_content,
                        note_source=note_source,
                        pubdate=pubdate,
                        publisher=publisher,
                        pubmonth=pubmonth,
                        format_urls=format_urls,
                        pubyear=pubyear,
                        rating=rating,
                        series=series,
                        series_index=series_index,
                        thumb_src=thumb_src,
                        title=title,
                        title_str=title_str,
                        languages=languages,
                        xmlns=XHTML_NS,
                        )
            for k, v in args.iteritems():
                if isbytestring(v):
                    args[k] = v.decode('utf-8')
            template = self.load_userfile_or_pluginfile('magic_catalog/magic_template.xhtml').decode('utf-8')
            generated_html = Templite(template).render(**args)
            generated_html = substitute_entites(generated_html)

            return BeautifulSoup(generated_html)

        # Generate the template arguments
        css = self.load_userfile_or_pluginfile('magic_catalog/magic_stylesheet.css').decode('utf-8')
        book_id = book['id']
        title_str = title = escape(book['title'])

        series = None
        series_index = None
        if book['series']:
            series = escape(book['series'])
            series_index = str(book['series_index'])
            if series_index.endswith('.0'):
                series_index = series_index[:-2]

        # Author, author_prefix (read|reading|none symbol or missing symbol)
        author = book['author']
        author_url = None
        # Insert the author link
        author_url = "%s.html#%s" % ("ByAuthor", self.generate_author_anchor(book['author']))

        # Languages
        languages = lang_as_iso639_1(book['languages'])

        # Genres
        genres = None
        if 'genres' in book and self.genre_tags_dict:
            genres = []
            for (i, tag) in enumerate(sorted(book.get('genres', []))):
                genres.append((i, tag, "Genre_%s.html" % self.genre_tags_dict[tag]))

        # Formats
        formats = None
        if 'formats' in book:
            formats = []
            for (i,format) in enumerate(sorted(book['formats'])):
                formats.append((i, format.rpartition('.')[2].upper()))

        # Date of publication
        if book['date']:
            pubdate = book['date']
            pubmonth, pubyear = pubdate.split()
        else:
            pubdate = pubyear = pubmonth = ''

        # Thumb
        if 'cover' in book and book['cover']:
            thumb_src = "../images/thumbnail_%d.jpg" % int(book['id'])
        else:
            thumb_src = "../images/thumbnail_default.jpg"

        # Publisher
        publisher = None
        if 'publisher' in book:
            publisher = book['publisher']

        # Rating
        stars = int(book['rating']) / 2
        rating = ''
        if stars:
            star_string = self.SYMBOL_FULL_RATING * stars
            empty_stars = self.SYMBOL_EMPTY_RATING * (5 - stars)
            rating = '%s%s' % (star_string, empty_stars)

        # Notes
        note_source = ''
        note_content = ''
        if 'notes' in book:
            note_source = book['notes']['source']
            note_content = book['notes']['content']

        #urls
	format_urls = None
        if 'formats' in book:
            format_urls = []
            for (i, f) in enumerate(sorted(book['formats'])):
                url = parse_library_url(self.opts.library_url)
                url = urljoin(url, pathname2url(os.path.relpath(f, self.library_base_path)))
                format_urls.append((i, f.rpartition('.')[2].upper(), url))

        # Comments
        comments = ''
        if 'description' in book and book['description'] > '':
            comments = book['description']

        # >>>> Populate the template <<<<
        soup = _generate_html()

        # >>>> Post-process the template <<<<
        body = soup.find('body')
        btc = 0
        # Insert the title anchor for inbound links
        aTag = Tag(soup, "a")
        aTag['id'] = "book%d" % int(book['id'])
        divTag = Tag(soup, 'div')
        divTag.insert(0, aTag)
        body.insert(btc, divTag)
        btc += 1

        # Insert the link to the series or remove <a class="series">
        aTag = body.find('a', attrs={'class': 'series_id'})
        if aTag:
            if book['series']:
                if self.opts.generate_series:
                    aTag['href'] = "%s.html#%s" % ('BySeries', self.generate_series_anchor(book['series']))
            else:
                aTag.extract()

        # Insert the author link
        aTag = body.find('a', attrs={'class': 'author'})
        if aTag:
            aTag['href'] = "%s.html#%s" % ("ByAuthor", self.generate_author_anchor(book['author']))

        if publisher == ' ':
            publisherTag = body.find('td', attrs={'class': 'publisher'})
            if publisherTag:
                publisherTag.contents[0].replaceWith('&nbsp;')

        if not genres:
            genresTag = body.find('p', attrs={'class': 'genres'})
            if genresTag:
                genresTag.extract()

        if not formats:
            formatsTag = body.find('p', attrs={'class': 'formats'})
            if formatsTag:
                formatsTag.extract()

        if note_content == '':
            tdTag = body.find('td', attrs={'class': 'notes'})
            if tdTag:
                tdTag.contents[0].replaceWith('&nbsp;')

        emptyTags = body.findAll('td', attrs={'class': 'empty'})
        for mt in emptyTags:
            newEmptyTag = Tag(BeautifulSoup(), 'td')
            newEmptyTag.insert(0, NavigableString('&nbsp;'))
            mt.replaceWith(newEmptyTag)

        return soup

    def generate_html_descriptions(self):
        """ Generate Description HTML for each book.

        Loop though books, write Description HTML for each book.

        Inputs:
         books_by_title (list)

        Output:
         (files): Description HTML for each book
        """

        self.update_progress_full_step(_("Descriptions HTML"))

        for (title_num, title) in enumerate(self.books_by_title):
            self.update_progress_micro_step("%s %d of %d" %
                                            (_("Description HTML"),
                                            title_num, len(self.books_by_title)),
                                            float(title_num * 100 / len(self.books_by_title)) / 100)

            # Generate the header from user-customizable template
            soup = self.generate_html_description_header(title)

            # Write the book entry to content_dir
            outfile = open("%s/book_%d.html" % (self.content_dir, int(title['id'])), 'w')
            outfile.write(soup.prettify())
            outfile.close()

    def generate_masthead_image(self, out_path):
        """ Generate a Kindle masthead image.

        Generate a Kindle masthead image, used with Kindle periodical format.

        Args:
         out_path (str): path to write generated masthead image

        Input:
         opts.catalog_title (str): Title to render
         masthead_font: User-specified font preference (MOBI output option)

        Output:
         out_path (file): masthead image (GIF)
        """

        from calibre.ebooks.conversion.config import load_defaults

        MI_WIDTH = 600
        MI_HEIGHT = 60

        font_path = default_font = P('fonts/liberation/LiberationSerif-Bold.ttf')
        recs = load_defaults('mobi_output')
        masthead_font_family = recs.get('masthead_font', 'Default')

        if masthead_font_family != 'Default':
            from calibre.utils.fonts.scanner import font_scanner
            faces = font_scanner.fonts_for_family(masthead_font_family)
            if faces:
                font_path = faces[0]['path']

        if not font_path or not os.access(font_path, os.R_OK):
            font_path = default_font

        try:
            from PIL import Image, ImageDraw, ImageFont
            Image, ImageDraw, ImageFont
        except ImportError:
            import Image, ImageDraw, ImageFont

        img = Image.new('RGB', (MI_WIDTH, MI_HEIGHT), 'white')
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(font_path, 48)
        except:
            self.opts.log.error("     Failed to load user-specifed font '%s'" % font_path)
            font = ImageFont.truetype(default_font, 48)
        text = self.opts.catalog_title.encode('utf-8')
        width, height = draw.textsize(text, font=font)
        left = max(int((MI_WIDTH - width) / 2.), 0)
        top = max(int((MI_HEIGHT - height) / 2.), 0)
        draw.text((left, top), text, fill=(0, 0, 0), font=font)
        img.save(open(out_path, 'wb'), 'GIF')

    def generate_ncx_header(self):
        """ Generate the basic NCX file.

        Generate the initial NCX, which is added to depending on included Sections.

        Inputs:
         None

        Updated:
         play_order (int)

        Outputs:
         ncx_soup (file): NCX foundation
        """

        self.update_progress_full_step(_("NCX header"))

        header = '''
            <?xml version="1.0" encoding="utf-8"?>
            <ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" xmlns:calibre="http://calibre.kovidgoyal.net/2009/metadata" version="2005-1" xml:lang="en">
            </ncx>
        '''
        soup = BeautifulStoneSoup(header, selfClosingTags=['content', 'calibre:meta-img'])

        ncx = soup.find('ncx')
        navMapTag = Tag(soup, 'navMap')

        # Build a top-level navPoint for Kindle periodicals
        navPointTag = Tag(soup, 'navPoint')
        navPointTag['class'] = "periodical"
        navPointTag['id'] = "title"
        navPointTag['playOrder'] = self.play_order
        self.play_order += 1
        navLabelTag = Tag(soup, 'navLabel')
        textTag = Tag(soup, 'text')
        textTag.insert(0, NavigableString(self.opts.catalog_title))
        navLabelTag.insert(0, textTag)
        navPointTag.insert(0, navLabelTag)

        contentTag = Tag(soup, 'content')
        contentTag['src'] = "content/ByAuthor.html"
        navPointTag.insert(1, contentTag)

        cmiTag = Tag(soup, '%s' % 'calibre:meta-img')
        cmiTag['id'] = "mastheadImage"
        cmiTag['src'] = "images/mastheadImage.gif"
        navPointTag.insert(2, cmiTag)
        navMapTag.insert(0, navPointTag)

        ncx.insert(0, navMapTag)
        self.ncx_soup = soup

    def generate_ncx_descriptions(self, tocTitle):
        """ Add Descriptions to the basic NCX file.

        Generate the Descriptions NCX content, add to self.ncx_soup.

        Inputs:
         books_by_author (list)

        Updated:
         play_order (int)

        Outputs:
         ncx_soup (file): updated
        """

        self.update_progress_full_step(_("NCX for Descriptions"))

        # --- Construct the 'Descriptions' section ---
        ncx_soup = self.ncx_soup
        body = ncx_soup.find("navPoint")
        btc = len(body.contents)

        # Add the section navPoint
        navPointTag = Tag(ncx_soup, 'navPoint')
        navPointTag['class'] = "section"
        navPointTag['id'] = "bydescription-ID"
        navPointTag['playOrder'] = self.play_order
        self.play_order += 1
        navLabelTag = Tag(ncx_soup, 'navLabel')
        textTag = Tag(ncx_soup, 'text')
        section_header = '%s [%d]' % (tocTitle, len(self.books_by_description))
        section_header = tocTitle
        textTag.insert(0, NavigableString(section_header))
        navLabelTag.insert(0, textTag)
        nptc = 0
        navPointTag.insert(nptc, navLabelTag)
        nptc += 1
        contentTag = Tag(ncx_soup, "content")
        contentTag['src'] = "content/book_%d.html" % int(self.books_by_description[0]['id'])
        navPointTag.insert(nptc, contentTag)
        nptc += 1

        # Loop over the titles

        for book in self.books_by_description:
            navPointVolumeTag = Tag(ncx_soup, 'navPoint')
            navPointVolumeTag['class'] = "article"
            navPointVolumeTag['id'] = "book%dID" % int(book['id'])
            navPointVolumeTag['playOrder'] = self.play_order
            self.play_order += 1
            navLabelTag = Tag(ncx_soup, "navLabel")
            textTag = Tag(ncx_soup, "text")
            if book['series']:
                series_index = str(book['series_index'])
                if series_index.endswith('.0'):
                    series_index = series_index[:-2]
                # Don't include Author for Kindle
                textTag.insert(0, NavigableString(self.format_ncx_text('%s (%s [%s])' %
                                                                       (book['title'], book['series'], series_index), dest='title')))
            else:
                # Don't include Author for Kindle
                title_str = self.format_ncx_text('%s' % (book['title']), dest='title')
                textTag.insert(0, NavigableString(title_str))
            navLabelTag.insert(0, textTag)
            navPointVolumeTag.insert(0, navLabelTag)

            contentTag = Tag(ncx_soup, "content")
            contentTag['src'] = "content/book_%d.html#book%d" % (int(book['id']), int(book['id']))
            navPointVolumeTag.insert(1, contentTag)

            # Add the author tag
            cmTag = Tag(ncx_soup, '%s' % 'calibre:meta')
            cmTag['name'] = "author"

            if book['date']:
                navStr = '%s | %s' % (self.format_ncx_text(book['author'], dest='author'),
                                      book['date'].split()[1])
            else:
                navStr = '%s' % (self.format_ncx_text(book['author'], dest='author'))

            if 'tags' in book and len(book['tags']):
                navStr = self.format_ncx_text(navStr + ' | ' + ' &middot; '.join(sorted(book['tags'])), dest='author')
            cmTag.insert(0, NavigableString(navStr))
            navPointVolumeTag.insert(2, cmTag)

            # Add the description tag
            if book['short_description']:
                cmTag = Tag(ncx_soup, '%s' % 'calibre:meta')
                cmTag['name'] = "description"
                cmTag.insert(0, NavigableString(self.format_ncx_text(book['short_description'], dest='description')))
                navPointVolumeTag.insert(3, cmTag)

            # Add this volume to the section tag
            navPointTag.insert(nptc, navPointVolumeTag)
            nptc += 1

        # Add this section to the body
        body.insert(btc, navPointTag)
        btc += 1

        self.ncx_soup = ncx_soup

    def generate_ncx_by_series(self, tocTitle):
        """ Add Series to the basic NCX file.

        Generate the Series NCX content, add to self.ncx_soup.

        Inputs:
         books_by_series (list)

        Updated:
         play_order (int)

        Outputs:
         ncx_soup (file): updated
        """

        self.update_progress_full_step(_("NCX for Series"))

        ncx_soup = self.ncx_soup
        HTML_file = "content/BySeries.html"
        body = ncx_soup.find("navPoint")
        btc = len(body.contents)

        # --- Construct the 'Books By Series' section ---
        navPointTag = Tag(ncx_soup, 'navPoint')
        navPointTag['class'] = "section"
        navPointTag['id'] = "byseries-ID"
        navPointTag['playOrder'] = self.play_order
        self.play_order += 1
        navLabelTag = Tag(ncx_soup, 'navLabel')
        textTag = Tag(ncx_soup, 'text')
        section_header = tocTitle
        textTag.insert(0, NavigableString(section_header))
        navLabelTag.insert(0, textTag)
        nptc = 0
        navPointTag.insert(nptc, navLabelTag)
        nptc += 1
        contentTag = Tag(ncx_soup, "content")
        contentTag['src'] = "%s#section_start" % HTML_file
        navPointTag.insert(nptc, contentTag)
        nptc += 1

        # Establish initial letter equivalencies
        #sort_equivalents = self.establish_equivalencies(self.books_by_series, key='series_sort')

        # Loop over the series titles, find start of each letter, add description_preview_count books
        # Special switch for using different title list

        serieses = self.generate_by_series_list(self.books_by_series)

        for idx, series in enumerate(serieses):
            navPointBySeriesTag = Tag(ncx_soup, 'navPoint')
            navPointBySeriesTag['class'] = "article"
            navPointBySeriesTag['id'] = "%sSeries-ID" % series['id']
            navPointTag['playOrder'] = self.play_order
            self.play_order += 1

            navLabelTag = Tag(ncx_soup, 'navLabel')
            textTag = Tag(ncx_soup, 'text')
            textTag.insert(0, NavigableString(series['name']))
            navLabelTag.insert(0, textTag)
            navPointBySeriesTag.insert(0, navLabelTag)
            contentTag = Tag(ncx_soup, 'content')
            contentTag['src'] = "%s#%s" % (HTML_file, series['id'])
            navPointBySeriesTag.insert(1, contentTag)

            cmTag = Tag(ncx_soup, '%s' % 'calibre:meta')
            cmTag['name'] = "description"
            cmTag.insert(0, NavigableString(self.format_ncx_text(series['name'], dest='description')))
            navPointBySeriesTag.insert(2, cmTag)

            navPointTag.insert(nptc, navPointBySeriesTag)
            nptc += 1

        # Add this section to the body
        body.insert(btc, navPointTag)
        btc += 1

        self.ncx_soup = ncx_soup

    def generate_ncx_by_author(self, tocTitle):
        """ Add Authors to the basic NCX file.

        Generate the Authors NCX content, add to self.ncx_soup.

        Inputs:
         authors (list)

        Updated:
         play_order (int)

        Outputs:
         ncx_soup (file): updated
        """

        self.update_progress_full_step(_("NCX for Authors"))

        ncx_soup = self.ncx_soup
        HTML_file = "content/ByAuthor.html"
        body = ncx_soup.find("navPoint")
        btc = len(body.contents)

        # --- Construct the 'Books By Author' *section* ---
        navPointTag = Tag(ncx_soup, 'navPoint')
        navPointTag['class'] = "section"
        file_ID = "%s" % tocTitle.lower()
        file_ID = file_ID.replace(" ", "")
        navPointTag['id'] = "%s-ID" % file_ID
        navPointTag['playOrder'] = self.play_order
        self.play_order += 1
        navLabelTag = Tag(ncx_soup, 'navLabel')
        textTag = Tag(ncx_soup, 'text')
        section_header = tocTitle
        textTag.insert(0, NavigableString(section_header))
        navLabelTag.insert(0, textTag)
        nptc = 0
        navPointTag.insert(nptc, navLabelTag)
        nptc += 1
        contentTag = Tag(ncx_soup, "content")
        contentTag['src'] = "%s#section_start" % HTML_file
        navPointTag.insert(nptc, contentTag)
        nptc += 1

        authors = self.generate_by_authors_list(self.books_by_author)

        for idx, author in enumerate(authors):
            navPointByAuthorTag = Tag(ncx_soup, 'navPoint')
            navPointByAuthorTag['class'] = "article"
            navPointByAuthorTag['id'] = "%sauthors-ID" % author['id']
            navPointTag['playOrder'] = self.play_order
            self.play_order += 1
            
            navLabelTag = Tag(ncx_soup, 'navLabel')
            textTag = Tag(ncx_soup, 'text')
            textTag.insert(0, NavigableString(author['name']))
            navLabelTag.insert(0, textTag)
            navPointByAuthorTag.insert(0, navLabelTag)
            contentTag = Tag(ncx_soup, 'content')
            contentTag['src'] = "%s#%s" % (HTML_file, author['id'])
            navPointByAuthorTag.insert(1, contentTag)

            cmTag = Tag(ncx_soup, '%s' % 'calibre:meta')
            cmTag['name'] = 'author'
            cmTag.insert(0, NavigableString(author['name']))
            navPointByAuthorTag.insert(2, cmTag)
            cmTag = Tag(ncx_soup, '%s' % 'calibre:meta')
            cmTag['name'] = 'description'
            cmTag.insert(0, NavigableString(author['name'])); # @@@mada
            navPointByAuthorTag.insert(3, cmTag);

            navPointTag.insert(nptc, navPointByAuthorTag)
            nptc += 1

        # Add this section to the body
        body.insert(btc, navPointTag)
        btc += 1

        self.ncx_soup = ncx_soup

    def generate_ncx_by_genre(self, tocTitle):
        """ Add Genres to the basic NCX file.

        Generate the Genre NCX content, add to self.ncx_soup.

        Inputs:
         genres (list)

        Updated:
         play_order (int)

        Outputs:
         ncx_soup (file): updated
        """

        self.update_progress_full_step(_("NCX for Genres"))

        if not len(self.genres):
            self.opts.log.warn(" No genres found\n"
                                " No Genre section added to Catalog")
            return

        ncx_soup = self.ncx_soup
        body = ncx_soup.find("navPoint")
        btc = len(body.contents)

        # --- Construct the 'Books By Genre' *section* ---
        navPointTag = Tag(ncx_soup, 'navPoint')
        navPointTag['class'] = "section"
        file_ID = "%s" % tocTitle.lower()
        file_ID = file_ID.replace(" ", "")
        navPointTag['id'] = "%s-ID" % file_ID
        navPointTag['playOrder'] = self.play_order
        self.play_order += 1
        navLabelTag = Tag(ncx_soup, 'navLabel')
        textTag = Tag(ncx_soup, 'text')
        section_header = tocTitle
        textTag.insert(0, NavigableString(section_header))
        navLabelTag.insert(0, textTag)
        nptc = 0
        navPointTag.insert(nptc, navLabelTag)
        nptc += 1
        contentTag = Tag(ncx_soup, "content")
        contentTag['src'] = "content/Genre_%s.html#section_start" % self.genres[0]['tag']
        navPointTag.insert(nptc, contentTag)
        nptc += 1

        for genre in self.genres:
            # Add an article for each genre
            navPointVolumeTag = Tag(ncx_soup, 'navPoint')
            navPointVolumeTag['class'] = "article"
            navPointVolumeTag['id'] = "genre-%s-ID" % genre['tag']
            navPointVolumeTag['playOrder'] = self.play_order
            self.play_order += 1
            navLabelTag = Tag(ncx_soup, "navLabel")
            textTag = Tag(ncx_soup, "text")

            # GwR *** Can this be optimized?
            normalized_tag = None
            for friendly_tag in self.genre_tags_dict:
                if self.genre_tags_dict[friendly_tag] == genre['tag']:
                    normalized_tag = self.genre_tags_dict[friendly_tag]
                    break
            textTag.insert(0, self.format_ncx_text(NavigableString(friendly_tag), dest='description'))
            navLabelTag.insert(0, textTag)
            navPointVolumeTag.insert(0, navLabelTag)
            contentTag = Tag(ncx_soup, "content")
            contentTag['src'] = "content/Genre_%s.html" % (normalized_tag)
            navPointVolumeTag.insert(1, contentTag)

            # Build the author tag
            cmTag = Tag(ncx_soup, '%s' % 'calibre:meta')
            cmTag['name'] = "author"
            # First - Last author

            if len(genre['titles_spanned']) > 1:
                author_range = "%s - %s" % (genre['titles_spanned'][0][0], genre['titles_spanned'][1][0])
            else:
                author_range = "%s" % (genre['titles_spanned'][0][0])

            cmTag.insert(0, NavigableString(author_range))
            navPointVolumeTag.insert(2, cmTag)

            # Build the description tag
            cmTag = Tag(ncx_soup, '%s' % 'calibre:meta')
            cmTag['name'] = "description"

            if False:
                # Form 1: Titles spanned
                if len(genre['titles_spanned']) > 1:
                    title_range = "%s -\n%s" % (genre['titles_spanned'][0][1], genre['titles_spanned'][1][1])
                else:
                    title_range = "%s" % (genre['titles_spanned'][0][1])
                cmTag.insert(0, NavigableString(self.format_ncx_text(title_range, dest='description')))
            else:
                # Form 2: title &bull; title &bull; title ...
                titles = []
                for title in genre['books']:
                    titles.append(title['title'])
                titles = sorted(titles, key=lambda x: (self.generate_sort_title(x), self.generate_sort_title(x)))
                titles_list = self.generate_short_description(u" &bull; ".join(titles), dest="description")
                cmTag.insert(0, NavigableString(self.format_ncx_text(titles_list, dest='description')))

            navPointVolumeTag.insert(3, cmTag)

            # Add this volume to the section tag
            navPointTag.insert(nptc, navPointVolumeTag)
            nptc += 1

        # Add this section to the body
        body.insert(btc, navPointTag)
        btc += 1
        self.ncx_soup = ncx_soup

    def generate_opf(self):
        """ Generate the OPF file.

        Start with header template, construct manifest, spine and guide.

        Inputs:
         genres (list)
         html_filelist_1 (list)
         html_filelist_2 (list)
         thumbs (list)

        Updated:
         play_order (int)

        Outputs:
         opts.basename + '.opf' (file): written
        """

        self.update_progress_full_step(_("Generating OPF"))

        header = '''
            <?xml version="1.0" encoding="UTF-8"?>
            <package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="calibre_id">
                <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf" xmlns:calibre="http://calibre.kovidgoyal.net/2009/metadata" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
                    <dc:language>en-US</dc:language>
                </metadata>
                <manifest></manifest>
                <spine toc="ncx"></spine>
                <guide></guide>
            </package>
            '''
        # Add the supplied metadata tags
        soup = BeautifulStoneSoup(header, selfClosingTags=['item', 'itemref', 'meta', 'reference'])
        metadata = soup.find('metadata')
        mtc = 0

        titleTag = Tag(soup, "dc:title")
        titleTag.insert(0, escape(self.opts.catalog_title))
        metadata.insert(mtc, titleTag)
        mtc += 1

        creatorTag = Tag(soup, "dc:creator")
        creatorTag.insert(0, self.opts.creator)
        metadata.insert(mtc, creatorTag)
        mtc += 1

        periodicalTag = Tag(soup, "meta")
        periodicalTag['name'] = "calibre:publication_type"
        periodicalTag['content'] = "periodical:default"
        metadata.insert(mtc, periodicalTag)
        mtc += 1

        # Create the OPF tags
        manifest = soup.find('manifest')
        mtc = 0
        spine = soup.find('spine')
        stc = 0
        guide = soup.find('guide')

        itemTag = Tag(soup, "item")
        itemTag['id'] = "ncx"
        itemTag['href'] = '%s.ncx' % self.opts.basename
        itemTag['media-type'] = "application/x-dtbncx+xml"
        manifest.insert(mtc, itemTag)
        mtc += 1

        itemTag = Tag(soup, "item")
        itemTag['id'] = 'stylesheet'
        itemTag['href'] = self.stylesheet
        itemTag['media-type'] = 'text/css'
        manifest.insert(mtc, itemTag)
        mtc += 1

        itemTag = Tag(soup, "item")
        itemTag['id'] = 'mastheadimage-image'
        itemTag['href'] = "images/mastheadImage.gif"
        itemTag['media-type'] = 'image/gif'
        manifest.insert(mtc, itemTag)
        mtc += 1

        # Write the thumbnail images, descriptions to the manifest
        for thumb in self.thumbs:
            itemTag = Tag(soup, "item")
            itemTag['href'] = "images/%s" % (thumb)
            end = thumb.find('.jpg')
            itemTag['id'] = "%s-image" % thumb[:end]
            itemTag['media-type'] = 'image/jpeg'
            manifest.insert(mtc, itemTag)
            mtc += 1

        # Add html_files to manifest and spine

        for file in self.html_filelist_1:
            # By Author, By Title, By Series,
            itemTag = Tag(soup, "item")
            start = file.find('/') + 1
            end = file.find('.')
            itemTag['href'] = file
            itemTag['id'] = file[start:end].lower()
            itemTag['media-type'] = "application/xhtml+xml"
            manifest.insert(mtc, itemTag)
            mtc += 1

            # spine
            itemrefTag = Tag(soup, "itemref")
            itemrefTag['idref'] = file[start:end].lower()
            spine.insert(stc, itemrefTag)
            stc += 1

        # Add genre files to manifest and spine
        for genre in self.genres:
            itemTag = Tag(soup, "item")
            start = genre['file'].find('/') + 1
            end = genre['file'].find('.')
            itemTag['href'] = genre['file']
            itemTag['id'] = genre['file'][start:end].lower()
            itemTag['media-type'] = "application/xhtml+xml"
            manifest.insert(mtc, itemTag)
            mtc += 1

            # spine
            itemrefTag = Tag(soup, "itemref")
            itemrefTag['idref'] = genre['file'][start:end].lower()
            spine.insert(stc, itemrefTag)
            stc += 1

        for file in self.html_filelist_2:
            # By Date Added, By Date Read
            itemTag = Tag(soup, "item")
            start = file.find('/') + 1
            end = file.find('.')
            itemTag['href'] = file
            itemTag['id'] = file[start:end].lower()
            itemTag['media-type'] = "application/xhtml+xml"
            manifest.insert(mtc, itemTag)
            mtc += 1

            # spine
            itemrefTag = Tag(soup, "itemref")
            itemrefTag['idref'] = file[start:end].lower()
            spine.insert(stc, itemrefTag)
            stc += 1

        for book in self.books_by_description:
            # manifest
            itemTag = Tag(soup, "item")
            itemTag['href'] = "content/book_%d.html" % int(book['id'])
            itemTag['id'] = "book%d" % int(book['id'])
            itemTag['media-type'] = "application/xhtml+xml"
            manifest.insert(mtc, itemTag)
            mtc += 1

            # spine
            itemrefTag = Tag(soup, "itemref")
            itemrefTag['idref'] = "book%d" % int(book['id'])
            spine.insert(stc, itemrefTag)
            stc += 1

        # Guide
        referenceTag = Tag(soup, "reference")
        referenceTag['type'] = 'masthead'
        referenceTag['title'] = 'mastheadimage-image'
        referenceTag['href'] = 'images/mastheadImage.gif'
        guide.insert(0, referenceTag)

        # Write the OPF file
        outfile = open("%s/%s.opf" % (self.catalog_path, self.opts.basename), 'w')
        outfile.write(soup.prettify())

    def generate_rating_string(self, book):
        """ Generate rating string for Descriptions.

        Starting with database rating (0-10), return 5 stars, with 0-5 filled,
        balance empty.

        Args:
         book (dict): book metadata

        Return:
         rating (str): 5 stars, 1-5 solid, balance empty. Empty str for no rating.
        """

        rating = ''
        try:
            if 'rating' in book:
                stars = int(book['rating']) / 2
                if stars:
                    star_string = self.SYMBOL_FULL_RATING * stars
                    empty_stars = self.SYMBOL_EMPTY_RATING * (5 - stars)
                    rating = '%s%s' % (star_string, empty_stars)
        except:
            # Rating could be None
            pass
        return rating

    def generate_series_anchor(self, series):
        """ Generate legal XHTML anchor for series names.

        Flatten series name to ascii_legal text.

        Args:
         series (str): series name

        Return:
         (str): asciized version of series name
        """

        # Generate a legal XHTML id/href string
        if self.letter_or_symbol(series) == self.SYMBOLS:
            return "symbol_%s_series" % re.sub('\W', '', series).lower()
        else:
            return "%s_series" % re.sub('\W', '', ascii_text(series)).lower()

    def generate_short_description(self, description, dest=None):
        """ Generate a truncated version of the supplied string.

        Given a string and NCX destination, truncate string to length specified
        in self.opts.

        Args:
         description (str): string to truncate
         dest (str): NCX destination
           description  NCX summary
           title        NCX title
           author       NCX author

        Return:
         (str): truncated description
        """

        def _short_description(description, limit):
            short_description = ""
            words = description.split()
            for word in words:
                short_description += word + " "
                if len(short_description) > limit:
                    short_description += "..."
                    return short_description

        if not description:
            return None

        if dest == 'title':
            # No truncation for titles, let the device deal with it
            return description
        elif dest == 'author':
            if self.opts.author_clip and len(description) < self.opts.author_clip:
                return description
            else:
                return _short_description(description, self.opts.author_clip)
        elif dest == 'description':
            if self.opts.description_clip and len(description) < self.opts.description_clip:
                return description
            else:
                return _short_description(description, self.opts.description_clip)
        else:
            print " returning description with unspecified destination '%s'" % description
            raise RuntimeError

    def generate_sort_title(self, title):
        """ Generates a sort string from title.

        Based on trunk title_sort algorithm, but also accommodates series
        numbers by padding with leading zeroes to force proper numeric
        sorting. Option to sort numbers alphabetically, e.g. '1942' sorts
        as 'Nineteen forty two'.

        Args:
         title (str):

        Return:
         (str): sort string
        """

        from calibre.ebooks.metadata import title_sort
        from calibre.library.catalogs.utils import NumberToText

        # Strip stop words
        title_words = title_sort(title).split()
        translated = []

        for (i, word) in enumerate(title_words):
            # Leading numbers optionally translated to text equivalent
            # Capitalize leading sort word
            if i == 0:
                # *** Keep this code in case we need to restore numbers_as_text ***
                if False:
                #if self.opts.numbers_as_text and re.match('[0-9]+',word[0]):
                    translated.append(NumberToText(word).text.capitalize())
                else:
                    if re.match('[0-9]+', word[0]):
                        word = word.replace(',', '')
                        suffix = re.search('[\D]', word)
                        if suffix:
                            word = '%10.0f%s' % (float(word[:suffix.start()]), word[suffix.start():])
                        else:
                            word = '%10.0f' % (float(word))

                    # If leading char > 'A', insert symbol as leading forcing lower sort
                    # '/' sorts below numbers, g
                    if self.letter_or_symbol(word[0]) != word[0]:
                        if word[0] > 'A' or (ord('9') < ord(word[0]) < ord('A')):
                            translated.append('/')
                    translated.append(capitalize(word))

            else:
                if re.search('[0-9]+', word[0]):
                    word = word.replace(',', '')
                    suffix = re.search('[\D]', word)
                    if suffix:
                        word = '%10.0f%s' % (float(word[:suffix.start()]), word[suffix.start():])
                    else:
                        word = '%10.0f' % (float(word))
                translated.append(word)
        return ' '.join(translated)

    def generate_thumbnail(self, title, image_dir, thumb_file):
        """ Create thumbnail of cover or return previously cached thumb.

        Test thumb archive for currently cached cover. Return cached version, or create
        and cache new version. Uses calibre.utils.magick.draw to generate thumbnail from
        cover.

        Args:
         title (dict): book metadata
         image_dir (str): directory to write thumb data to
         thumb_file (str): filename to save thumb as

        Output:
         (file): thumb written to /images
         (archive): current thumb archived under cover crc
        """

        def _open_archive(mode='r'):
            try:
                return ZipFile(self.thumbs_path, mode=mode, allowZip64=True)
            except:
                # occurs under windows if the file is opened by another
                # process
                pass

        # Generate crc for current cover
        with open(title['cover'], 'rb') as f:
            data = f.read()
        cover_crc = hex(zlib.crc32(data))

        # Test cache for uuid
        zf = _open_archive()
        if zf is not None:
            with zf:
                try:
                    zf.getinfo(title['uuid'] + cover_crc)
                except:
                    pass
                else:
                    # uuid found in cache with matching crc
                    thumb_data = zf.read(title['uuid'] + cover_crc)
                    with open(os.path.join(image_dir, thumb_file), 'wb') as f:
                        f.write(thumb_data)
                    return

        # Save thumb for catalog. If invalid data, error returns to generate_thumbnails()
        thumb_data = thumbnail(data,
                width=self.thumb_width, height=self.thumb_height)[-1]
        with open(os.path.join(image_dir, thumb_file), 'wb') as f:
            f.write(thumb_data)

        # Save thumb to archive
        if zf is not None:
            # Ensure that the read succeeded
            # If we failed to open the zip file for reading,
            # we dont know if it contained the thumb or not
            zf = _open_archive('a')
            if zf is not None:
                with zf:
                    zf.writestr(title['uuid'] + cover_crc, thumb_data)

    def generate_thumbnails(self):
        """ Generate a thumbnail cover for each book.

        Generate or retrieve a thumbnail for each cover. If nonexistent or faulty
        cover data, substitute default cover. Checks for updated default cover.
        At completion, writes self.opts.thumb_width to archive.

        Inputs:
         books_by_title (list): books to catalog

        Output:
         thumbs (list): list of referenced thumbnails
        """

        self.update_progress_full_step(_("Thumbnails"))
        thumbs = ['thumbnail_default.jpg']
        image_dir = "%s/images" % self.catalog_path
        for (i, title) in enumerate(self.books_by_title):
            # Update status
            self.update_progress_micro_step("%s %d of %d" %
                (_("Thumbnail"), i, len(self.books_by_title)),
                 i / float(len(self.books_by_title)))

            thumb_file = 'thumbnail_%d.jpg' % int(title['id'])
            thumb_generated = True
            valid_cover = True
            try:
                self.generate_thumbnail(title, image_dir, thumb_file)
                thumbs.append("thumbnail_%d.jpg" % int(title['id']))
            except:
                if 'cover' in title and os.path.exists(title['cover']):
                    valid_cover = False
                    self.opts.log.warn(" *** Invalid cover file for '%s'***" %
                                            (title['title']))
                    if not self.error:
                        self.error.append('Invalid cover files')
                    self.error.append("Warning: invalid cover file for '%s', default cover substituted.\n" % (title['title']))

                thumb_generated = False

            if not thumb_generated:
                self.opts.log.warn("     using default cover for '%s' (%d)" % (title['title'], title['id']))
                # Confirm thumb exists, default is current
                default_thumb_fp = os.path.join(image_dir, "thumbnail_default.jpg")
                cover = os.path.join(self.catalog_path, "DefaultCover.png")
                title['cover'] = cover

                if not os.path.exists(cover):
                    shutil.copyfile(I('book.png'), cover)

                if os.path.isfile(default_thumb_fp):
                    # Check to see if default cover is newer than thumbnail
                    # os.path.getmtime() = modified time
                    # os.path.ctime() = creation time
                    cover_timestamp = os.path.getmtime(cover)
                    thumb_timestamp = os.path.getmtime(default_thumb_fp)
                    if thumb_timestamp < cover_timestamp:
                        if self.DEBUG and self.opts.verbose:
                            self.opts.log.warn("updating thumbnail_default for %s" % title['title'])
                        self.generate_thumbnail(title, image_dir,
                                            "thumbnail_default.jpg" if valid_cover else thumb_file)
                else:
                    if self.DEBUG and self.opts.verbose:
                        self.opts.log.warn("     generating new thumbnail_default.jpg")
                    self.generate_thumbnail(title, image_dir,
                                            "thumbnail_default.jpg" if valid_cover else thumb_file)
                # Clear the book's cover property
                title['cover'] = None

        # Write thumb_width to the file, validating cache contents
        # Allows detection of aborted catalog builds
        with ZipFile(self.thumbs_path, mode='a') as zfw:
            zfw.writestr('thumb_width', self.opts.thumb_width)

        self.thumbs = thumbs

    def generate_unicode_name(self, c):
        """ Generate a legal XHTML anchor from unicode character.

        Generate a legal XHTML anchor from unicode character.

        Args:
         c (unicode): character(s)

        Return:
         (str): legal XHTML anchor string of unicode character name
        """
        fullname = u''.join(unicodedata.name(unicode(cc)) for cc in c)
        terms = fullname.split()
        return "_".join(terms)

    def get_excluded_tags(self):
        """ Get excluded_tags from opts.exclusion_rules.

        Parse opts.exclusion_rules for tags to be excluded, return list.
        Log books that will be excluded by excluded_tags.

        Inputs:
         opts.excluded_tags (tuples): exclusion rules

        Return:
         excluded_tags (list): excluded tags
        """
        # Remove dups
        excluded_tags = [_('Catalog')]

        # Report excluded books
        if self.opts.verbose and excluded_tags:
            self.opts.log.info(" Books excluded by tag:")
            data = self.db.get_data_as_dict(ids=self.opts.ids)
            for record in data:
                matched = list(set(record['tags']) & set(excluded_tags))
                if matched:
                    for tag in self.opts.exclusion_tags:
                        if tag == str(matched[0]):
                            self.opts.log.info("  - '%s' by %s (Exclusion Tag '%s')" %
                                (record['title'], record['authors'][0], tag))

        return excluded_tags

    def get_friendly_genre_tag(self, genre):
        """ Return the first friendly_tag matching genre.

        Scan self.genre_tags_dict[] for first friendly_tag matching genre.
       genre_tags_dict[] populated in filter_genre_tags().

        Args:
         genre (str): genre to match

        Return:
         friendly_tag (str): friendly_tag matching genre
        """
        # Find the first instance of friendly_tag matching genre
        for friendly_tag in self.genre_tags_dict:
            if self.genre_tags_dict[friendly_tag] == genre:
                return friendly_tag

    def get_output_profile(self, _opts):
        """ Return profile matching opts.output_profile

        Input:
         _opts (object): build options object

        Return:
         (profile): output profile matching name
        """
        for profile in output_profiles():
            if profile.short_name == _opts.output_profile:
                return profile

    def letter_or_symbol(self, char):
        """ Test asciized char for A-z.

        Convert char to ascii, test for A-z.

        Args:
         char (chr): character to test

        Return:
         (str): char if A-z, else SYMBOLS
        """
        if not re.search('[a-zA-Z]', ascii_text(char)):
            return self.SYMBOLS
        else:
            return char

    def massage_comments(self, comments):
        """ Massage comments to somewhat consistent format.

        Convert random comment text to normalized, xml-legal block of <p>s
        'plain text' returns as
        <p>plain text</p>

        'plain text with <i>minimal</i> <b>markup</b>' returns as
        <p>plain text with <i>minimal</i> <b>markup</b></p>

        '<p>pre-formatted text</p> returns untouched

        'A line of text\n\nFollowed by a line of text' returns as
        <p>A line of text</p>
        <p>Followed by a line of text</p>

        'A line of text.\nA second line of text.\rA third line of text' returns as
        <p>A line of text.<br />A second line of text.<br />A third line of text.</p>

        '...end of a paragraph.Somehow the break was lost...' returns as
        <p>...end of a paragraph.</p>
        <p>Somehow the break was lost...</p>

        Deprecated HTML returns as HTML via BeautifulSoup()

        Args:
         comments (str): comments from metadata, possibly HTML

        Return:
         result (BeautifulSoup): massaged comments in HTML form
        """

        # Hackish - ignoring sentences ending or beginning in numbers to avoid
        # confusion with decimal points.

        # Explode lost CRs to \n\n
        for lost_cr in re.finditer('([a-z])([\.\?!])([A-Z])', comments):
            comments = comments.replace(lost_cr.group(),
                                        '%s%s\n\n%s' % (lost_cr.group(1),
                                                        lost_cr.group(2),
                                                        lost_cr.group(3)))
        # Extract pre-built elements - annotations, etc.
        if not isinstance(comments, unicode):
            comments = comments.decode('utf-8', 'replace')
        soup = BeautifulSoup(comments)
        elems = soup.findAll('div')
        for elem in elems:
            elem.extract()

        # Reconstruct comments w/o <div>s
        comments = soup.renderContents(None)

        # Convert \n\n to <p>s
        if re.search('\n\n', comments):
            soup = BeautifulSoup()
            split_ps = comments.split(u'\n\n')
            tsc = 0
            for p in split_ps:
                pTag = Tag(soup, 'p')
                pTag.insert(0, p)
                soup.insert(tsc, pTag)
                tsc += 1
            comments = soup.renderContents(None)

        # Convert solo returns to <br />
        comments = re.sub('[\r\n]', '<br />', comments)

        # Convert two hypens to emdash
        comments = re.sub('--', '&mdash;', comments)
        soup = BeautifulSoup(comments)
        result = BeautifulSoup()
        rtc = 0
        open_pTag = False

        all_tokens = list(soup.contents)
        for token in all_tokens:
            if type(token) is NavigableString:
                if not open_pTag:
                    pTag = Tag(result, 'p')
                    open_pTag = True
                    ptc = 0
                pTag.insert(ptc, prepare_string_for_xml(token))
                ptc += 1

            elif token.name in ['br', 'b', 'i', 'em']:
                if not open_pTag:
                    pTag = Tag(result, 'p')
                    open_pTag = True
                    ptc = 0
                pTag.insert(ptc, token)
                ptc += 1

            else:
                if open_pTag:
                    result.insert(rtc, pTag)
                    rtc += 1
                    open_pTag = False
                    ptc = 0
                # Clean up NavigableStrings for xml
                sub_tokens = list(token.contents)
                for sub_token in sub_tokens:
                    if type(sub_token) is NavigableString:
                        sub_token.replaceWith(prepare_string_for_xml(sub_token))
                result.insert(rtc, token)
                rtc += 1

        if open_pTag:
            result.insert(rtc, pTag)
            rtc += 1

        paras = result.findAll('p')
        for p in paras:
            p['class'] = 'description'

        # Add back <div> elems initially removed
        for elem in elems:
            result.insert(rtc, elem)
            rtc += 1

        return result.renderContents(encoding=None)

    def merge_comments(self, record):
        """ Merge comments with custom column content.

        Merge comments from book metadata with user-specified custom column
         content, optionally before or after. Optionally insert <hr> between
         fields.

        Args:
         record (dict): book metadata

        Return:
         merged (str): comments merged with addendum
        """

        merged = ''
        if record['description']:
            merged = record['description']
            merged += '\n'
        else:
            merged = ''

        return merged

    def relist_multiple_authors(self, books_by_author):
        """ Create multiple entries for books with multiple authors

        Given a list of books by author, scan list for books with multiple
        authors. Add a cloned copy of the book per additional author.

        Args:
         books_by_author (list): book list possibly containing books
         with multiple authors

        Return:
         (list): books_by_author with additional cloned entries for books with
         multiple authors
        """

        multiple_author_books = []

        # Find the multiple author books
        for book in books_by_author:
            if len(book['authors']) > 1:
                multiple_author_books.append(book)

        for book in multiple_author_books:
            cloned_authors = list(book['authors'])
            for x, author in enumerate(book['authors']):
                if x:
                    first_author = cloned_authors.pop(0)
                    cloned_authors.append(first_author)
                    new_book = deepcopy(book)
                    new_book['author'] = ' & '.join(cloned_authors)
                    new_book['authors'] = list(cloned_authors)
                    asl = [author_to_author_sort(auth) for auth in cloned_authors]
                    new_book['author_sort'] = ' & '.join(asl)
                    books_by_author.append(new_book)

        return books_by_author

    def update_progress_full_step(self, description):
        """ Update calibre's job status UI.

        Call ProgessReporter() with updates.

        Args:
         description (str): text describing current step

        Result:
         (UI): Jobs UI updated
        """

        self.current_step += 1
        self.progress_string = description
        self.progress_int = float((self.current_step - 1) / self.total_steps)
        if not self.progress_int:
            self.progress_int = 0.01
        self.reporter(self.progress_int, self.progress_string)
        if self.opts.cli_environment:
            self.opts.log(u"%3.0f%% %s" % (self.progress_int * 100, self.progress_string))

    def update_progress_micro_step(self, description, micro_step_pct):
        """ Update calibre's job status UI.

        Called from steps requiring more time:
         generate_html_descriptions()
         generate_thumbnails()

        Args:
         description (str): text describing microstep
         micro_step_pct (float): percentage of full step

        Results:
         (UI): Jobs UI updated
        """

        step_range = 100 / self.total_steps
        self.progress_string = description
        coarse_progress = float((self.current_step - 1) / self.total_steps)
        fine_progress = float((micro_step_pct * step_range) / 100)
        self.progress_int = coarse_progress + fine_progress
        self.reporter(self.progress_int, self.progress_string)

    def write_ncx(self):
        """ Write accumulated ncx_soup to file.

        Expanded description

        Inputs:
         catalog_path (str): path to generated catalog
         opts.basename (str): catalog basename

        Output:
         (file): basename.NCX written
        """
        self.update_progress_full_step(_("Saving NCX"))

        outfile = open("%s/%s.ncx" % (self.catalog_path, self.opts.basename), 'w')
        outfile.write(self.ncx_soup.prettify())

    def load_userfile_or_pluginfile(self, name):
        """ load file from user configuration directory or plugin-zip.

        Args:
         name  (str): load file name

        Return: loaded file
        """
        user_path = os.path.join(config_dir, 'resources')
        srcpath = os.path.join(user_path, name)
        data = None
        if os.path.exists(srcpath):
            with open(srcpath, 'rb') as f:
                data = f.read()
        else:
            ans = self.plugin.load_resources([name])
            data = ans[name]
        return data
