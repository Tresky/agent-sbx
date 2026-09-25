# GEOS, GDAL and PROJ, for PostGIS, RGeo and GeoDjango

step "gis libraries"
# GEOS, GDAL and PROJ: what the PostGIS adapter, RGeo and GeoDjango link.
apt-get install -y -q --no-install-recommends libgeos-dev gdal-bin libgdal-dev libproj-dev
