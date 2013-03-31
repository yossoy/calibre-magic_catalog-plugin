PLUGIN=../calibre_magic_mobi_plugin.zip
DEBUGFILE=debug.mobi
all: $(PLUGIN)

FILES= \
	__init__.py	\
	about.txt	\
	catalog_magic_mobi.py	\
	generate.py	\
	catalog_magic_mobi.py	\
	catalog_magic_mobi.ui	\
	catalog_magic_mobi_ui.py	\
	plugin-import-name-magic_mobi.txt \
	magic_catalog/DefaultCover.jpg \
	magic_catalog/mastheadImage.gif \
	magic_catalog/magic_stylesheet.css \
	magic_catalog/magic_template.xhtml \
	magic_catalog/magic_author_template.xhtml \
	magic_catalog/magic_series_template.xhtml

PYUIC4=python c:/Python27/lib/site-packages/PyQt4/uic/pyuic.py
CALIBRE_PATH=c:/Program Files/Calibre2/
CATALOGOPT= \
	--output-profile=kindle

$(PLUGIN): $(FILES)
	$(RM) $@
	zip -r $@ $^

catalog_magic_mobi_ui.py: catalog_magic_mobi.ui
	$(PYUIC4) $^ > $@

add::
	"$(CALIBRE_PATH)calibre-customize" -a $(PLUGIN)

remove::
	"$(CALIBRE_PATH)calibre-customize" -r Catalog_MAGIC_MOBI

debugui::
	"$(CALIBRE_PATH)calibre-debug" -g

debug::
	"$(CALIBRE_PATH)calibredb" catalog $(DEBUGFILE) mobi --verbose $(CATALOGOPT)

clean::
	-$(RM) $(PLUGIN) $(DEBUGFILE)



