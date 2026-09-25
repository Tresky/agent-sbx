#!/bin/sh
# Builds the DHCP client again on every link but loopback, so that the next
# request carries the CURRENT hostname. See sbx-dhcp-hostname.service.
#
# `networkctl renew` is not enough: it repeats the request that the client
# already holds, with the hostname the client read when the link came up. On
# a first boot that is the image's own name, "ubuntu", because the first
# request leaves before cloud-init sets the hostname. The real gateway showed
# exactly that: its lease for the template build VM was named "ubuntu".
for link in $(networkctl list --no-legend | awk '$2 != "lo" {print $2}'); do
  networkctl reconfigure "$link" || true
done
