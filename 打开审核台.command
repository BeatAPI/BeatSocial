#!/bin/sh
# Double-click to open the BeatSocial review desk.
DIR="$(cd "$(dirname "$0")" && pwd)"
(sleep 1 && open "http://127.0.0.1:9361/") &
exec "$DIR/bin/beatsocial" serve
