Generated silence, no external audio. Created with FFmpeg:

    ffmpeg -f lavfi -i anullsrc=r=44100:cl=stereo -t 4 -c:a aac -b:a 128k -f dash -seg_duration 2 -use_template 1 -use_timeline 0 audio.mpd

These tiny initialization/fragments exercise actual MP4 box layouts, duration patching, global indexing and range assembly. The DASH manifest is not used by the tests.
