# An optional part of the template. provision.sh sources this file when
# SBX_TEMPLATE_EXTRAS names it. It runs as root, after the core packages, with
# step, as_user, $U and the ERR trap of provision.sh. Add a tool name to
# CHECK_TOOLS: the final check then proves the tool is on the user's PATH.

step "gis libraries"
# GEOS, GDAL and PROJ: what the PostGIS adapter, RGeo and GeoDjango link.
apt-get install -y -q --no-install-recommends libgeos-dev gdal-bin libgdal-dev libproj-dev
