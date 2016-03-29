# mythtvjob-transcode-hcli-h264
Mythtv User job that allows for easy conversion and replacement of the source file w/ a mp4.

This script depends on:
* Mythtv
* HandBrakeCLI
* mediainfo
* AtomicParsley
 
The script can be run as a UserJob in Mythtv or as a commandline script
* /path/to/script/transcode-hcli-h264.py %JOBID% (optional flags: --sd=1, --burncc=1)
* /path/to/script/transcode-hcli-h264.py --chanid=XXXX --starttime=XXXXXXXXXXXXXXXX --txoffset=X (optional flags: --sd=1, --burncc=1, -v VERBOSE)
 
