# ffmpeg, for audio and video work and for screen recordings of browser tests

step "media tools"
apt-get install -y -q --no-install-recommends ffmpeg
CHECK_TOOLS+=" ffmpeg"
