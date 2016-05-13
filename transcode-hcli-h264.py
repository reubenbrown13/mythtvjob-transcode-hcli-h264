#!/usr/bin/env python
# -*- coding: UTF-8 -*-

# 2015 Michael Stucky
#
# This script is based on Raymond Wagner's transcode wrapper stub.
# Designed to be a USERJOB of the form </path to script/transcode-h264.py %JOBID%>
#
# Modifications - Drew 7/5/2015
#
# - chanid/starttime arguments needed a couple fixes to get it working
# - added ability to cut out commercials before transcode via generate_commcutlist=True
# - added ability to implement a compression ratio
#         the compression ratio estimates the input streams encoding bitrate (all streams as a one bitrate)
#         then computes an output bitrate as a factor of this bitrate, i.e., if compressionRatio=0.75
#         then the output video will be encoded at 75% of the input video bitrate. Usually one sets
#         compressionRatio < 1, resulting in a smaller output file.
#         Note the estimated bitrate is derived from the video duration
#         and file size hence it will over estimate the true video bitrate as it does not account for the
#         encapsulation overhead of the input encoding scheme nor the bitrate of any included audio streams.
#         To enable, set estimateBitrate = True and set compressionRatio to your desired value (I use 0.7). 
# - added ability to change h264 encoding preset and constant rate factor (crf) settings for HD video
# - added loads of debug statements, I've kept them in to facilitate hacking -- sorry to the purists in advance
# - added status output from HandBrakeCLI to the myth backend giving % complete, ETA and fps encode statistics.
# - added "smart" commercial detection - if needed it is run and on completion cancels any other mythcommflag jobs 
#         for the transcoded recording
# Modifications - Drew 1/25/2016
# - added fix for the markup data which is inaccurate, especially when commercials are removed
# Modifications - Reuben 3/26/2016
# - added fix for detection of framerate and dimentions to use mediainfo and also allow for video scaling and 
#         maxWidth/maxHeight to be setup.
# - added subtitles to be saved in the file
# - added output file name to be of show title and either startdate, airdate, or Season/Episode
#
from MythTV import Job, Recorded, System, MythDB, findfile, MythError, MythLog, DBDataWrite, datetime

from optparse import OptionParser
from glob import glob
from shutil import copyfile
import linecache
import sys
import os
import subprocess
import errno
import threading, time
from datetime import timedelta
import re, tempfile
import Queue # thread-safe
########## IMPORTANT #####################
#
# YOU WILL NEED TO EDIT THE SETTINGS BELOW
#
########## IMPORTANT #####################

transcoder = '/usr/bin/HandBrakeCLI'
mediainfo = '/usr/bin/mediainfo'
# If desired, set the export folder for when overWrite = 0
exportFolder = '/var/lib/mythtv/mythexport'

# flush_commskip
#       True => (Default) the script will delete all commercial skip indices from the old file 
#      False => the transcode will leave the commercial skip indices from the old file "as is" 
flush_commskip = False

# require_commflagged
#       True => the script will ensure mythcommflag has run on the file before encoding it
#      False => (Default) the transcode will process the video file "as is" 
require_commflagged = False

# generate_commcutlist 
#       True => (Default) flagged commercials are removed from the output video file
#      False => flagged commercials are NOT removed from the output video file
generate_commcutlist = False

# estimateBitrate 
#       True => (Default) the bitrate of the input file is estimated via size & duration
#               ** Required True for "compressionRatio" option to work.
#      False => The bitrate of the input file is unknown 
estimateBitrate = True

# compressionRatio
#      0.0 - 1.0 => Set the approximate bitrate of the output relative to the 
#                   detected bitrate of the input.
#                   One can think of this as the target compression rate, i.e., the 
#                   compressionRatio = (output filesize)/(input filesize)
#                   h264 video quality is approximately equal to mpeg2 video quality
#                   at a compression ratio of 0.65-0.75
#                   * Note: When enabled, this value will determine the approximate 
#                     relative size of the output file and input file
#                   (output filesize) = compressionRatio * (input filesize)
compressionRatio = 0.65

# enforce a max (do not exceed) bitrate for encoded HD video
# to disable set hd_max_bitrate=0
hdvideo_max_bitrate = 5500  # 0 = disable or (kBits_per_sec,kbps)
hdvideo_min_bitrate = 0     # 0 = disable or (kBits_per_sec,kbps)

# set max width and height for HD programs
maxWidth = 1280
maxHeight = 720

# number of seconds of video that can be held in playing device video buffers (typically 2-5 secs)
NUM_SECS_VIDEO_BUF=3        # secs
device_bufsize = NUM_SECS_VIDEO_BUF*hdvideo_max_bitrate # (kBits_per_sec,kbps)

# enforce a target bitrate for the encoder to achieve approximately 
#hdvideo_tgt_bitrate = 5000   # 0 = disable or (kBits_per_sec,kbps)
hdvideo_tgt_bitrate = 0   # 0 = disable or (kBits_per_sec,kbps)

# build_seektable
#       True => Rebuild myth seek table.
#               It allows accurate ffwd,rew / seeking on the transcoded output video
#      False => (Default) Do not rebuild the myth seek table. Not working for mythtv on h264 content.
build_seektable = True

# Making this true enables a bunch of debug information to be printed as the script runs.
debug = True

# TODO - override buffer size (kB), only use when necessary for a specific target device
# bufsize_override=0       # 0 = disable or (kBits_per_sec,kbps)
# h264 encode preset
# ultrafast,superfast, veryfast, faster, fast, medium, slow, slower, veryslow
preset_HD = 'fast'
preset_nonHD = 'slow'

# h264 encode constant rate factor (used for non-HD) valid/sane values 18-28
# lower values -> higher quality, larger output files,
# higher values -> lower quality, smaller output files
crf = '21'

# if HD, copy input audio streams to the output audio streams
abitrate_param_HD='--aencoder av_aac,copy:ac3'

# if non-HD, encode audio to AAC with libfdk_aac at a bitrate of 128kbps
abitrate_param_nonHD = '--aencoder copy:aac --mixdown stereo'

# to convert non-HD audio to AAC using HandBrakeCLI's aac encoder	
#abitrate_param_nonHD='-strict -2'

# TODO use -crf 20 -maxrate 400k -bufsize 1835k
# effectively "target" crf 20, but if the output exceeds 400kb/s, it will degrade to something more than crf 20
# TODO detect and preserve ac3 5.1 streams typically found in HD content
# TODO detect and preserve audio streams by language 
# TODO detect and preserve subtitle streams by language 
# TODO is m4v or mkv better for subtitle support in playback
#   subtitle codecs for MKV containers: copy, ass, srt, ssa
#   subtitle codecs for M4V containers: copy, mov_text

# Languages for audio stream and subtitle selection
# eng - English
# fre - French
# ger - German
# ita - Italian
# spa - Spanish
language = 'eng'

# interval between reads from the HandBrakeCLI status file 
# also defines the interval when waiting for a mythcommflag job to finish 
POLL_INTERVAL=30 # secs
# mythtv automatically launched user jobs with nice level of 17 
# this will add to that level (only positive values allowed unless run as root)
# e.g., NICELEVEL=1 will run with a nice level of 18. The max nicelevel is 19.
#NICELEVEL=5
NICELEVEL=0

class CleanExit:
  pass

def runjob(jobid=None, chanid=None, starttime=None, tzoffset=None, maxWidth=maxWidth, maxHeight=maxHeight, sdonly=0, burncc=0, usemkv=0, overwrite=1):
    global estimateBitrate
    db = MythDB()
    
    try:
        if jobid:
            job = Job(jobid, db=db)
            chanid = job.chanid
            utcstarttime = job.starttime
        else:
            job=None;
            utcstarttime = datetime.strptime(starttime, "%Y%m%d%H%M%S")
	    utcstarttime = utcstarttime + timedelta(hours=tzoffset)

        if debug:
            print 'chanid "%s"' % chanid
            print 'utcstarttime "%s"' % utcstarttime

        rec = Recorded((chanid, utcstarttime), db=db);
        
        utcstarttime = rec.starttime;
        starttime_datetime = utcstarttime
       
        # reformat 'starttime' for use with mythtranscode/HandBrakeCLI/mythcommflag
        starttime = str(utcstarttime.utcisoformat().replace(u':', '').replace(u' ', '').replace(u'T', '').replace('-', ''))
        if debug:
            print 'mythtv format starttime "%s"' % starttime
        input_filesize = rec.filesize
        
        if rec.commflagged:
            if debug:
                print 'Recording has been scanned to detect commerical breaks.'
            waititer=1
            keepWaiting = True
            while keepWaiting == True:
                keepWaiting=False;
                for index,jobitem in reversed(list(enumerate(db.searchJobs(chanid=chanid, starttime=starttime_datetime)))):
                    if jobitem.type == jobitem.COMMFLAG:  # Commercial flagging job
                        if debug:
	                    print 'Commercial flagging job detected with status %s' % jobitem.status
                        if jobitem.status == jobitem.RUNNING: # status = RUNNING?
                            job.update({'status':job.PAUSED, 
                                        'comment':'Waited %d secs for the commercial flagging job' % (waititer*POLL_INTERVAL) \
                                         + ' currently running on this recording to complete.'})
                            if debug:
                                print 'Waited %d secs for the commercial flagging job' % (waititer*POLL_INTERVAL) \
                                      + ' currently running on this recording to complete.'
                            time.sleep(POLL_INTERVAL);
                            keepWaiting=True
                            waititer = waititer + 1
                            break
        else:
            if debug:
                print 'Recording has not been scanned to detect/remove commercial breaks.'
            if require_commflagged:
                if jobid:
                    job.update({'status':job.RUNNING, 'comment':'Required commercial flagging for this file is not found.'
                                + 'Flagging commercials and cancelling any queued commercial flagging.'})
                # cancel any queued job to flag commercials for this recording and run commercial flagging in this script
                for index,jobitem in reversed(list(enumerate(db.searchJobs(chanid=chanid,starttime=starttime_datetime)))):
                    if debug:
                        if index==0:
                            print jobitem.keys()
                        print index,jobitem.id,jobitem.chanid

                    if jobitem.type == jobitem.COMMFLAG:  # Commercial flagging job
                        if jobitem.status == jobitem.RUNNING: # status = RUNNING?
                            jobitem.cmds = jobitem.STOP # stop command from the frontend to stop the commercial flagging job
                        #jobitem.setStatus(jobitem.CANCELLED)
                        #jobitem.setComment('Cancelled: Transcode command ran commercial flagging for this recording.')
                        jobitem.update({'status':jobitem.CANCELLED, 
                                        'comment':'A user transcode job ran commercial flagging for'
                                        + ' this recording and cancelled this job.'})
                if debug:
                    print 'Flagging Commercials...'
                # Call "mythcommflag --chanid $CHANID --starttime $STARTTIME"
                task = System(path='mythcommflag', db=db)
                try:
                    output = task('--chanid "%s"' % chanid,
                                  '--starttime "%s"' % starttime,
                                  '2> /dev/null')
                except MythError, e:
                    # it seems mythcommflag always exits with an decoding error "eno: Unknown error 541478725 (541478725)"
                    pass
                    #print 'Command failed with output:\n%s' % e.stderr
                    #if jobid:
                    #    job.update({'status':304, 'comment':'Flagging commercials failed'})
                    #sys.exit(e.retcode)

        sg = findfile('/'+rec.basename, rec.storagegroup, db=db)
        if sg is None:
            print 'Local access to recording not found.'
            sys.exit(1)

        infile = os.path.join(sg.dirname, rec.basename)
        #TODO: set overWrite to 0 if infile is m4v or mkv (already converted)
        #tmpfile = '%s.tmp' % infile.rsplit('.',1)[0]
        outtitle = rec.title.replace("&", "and")
        outtitle = re.sub('[^A-Za-z0-9 ]+', '', outtitle)
        filetype = 'm4v'
        if usemkv == 1:
            filetype = 'mkv'
        #print '!{}!'.format(rec.programid[0:2])
        if rec.season > 0 and rec.episode > 0: #if there are seasons and episode numbers in the recording data
            outtitle = '{0:s} S{1:d} E{2:02d}'.format(outtitle, rec.season, rec.episode)
        elif rec.programid[0:2] == 'MV' and rec.originalairdate > datetime.date(datetime(1, 1, 1, 0, 0)): #if it's a movie and has an original air date for when it came out
            outtitle = '{} ({})'.format(outtitle, rec.originalairdate.year)
        elif 'Sports' in rec.category: #if it's sports
			outtitle = '{}-{}-{}'.format(outtitle, re.sub('[^A-Za-z0-9 ]+', '', rec.subtitle), str(rec.starttime.strftime("%Y%m%d")))
        elif rec.programid[0:2] == 'SH' and (' News ' in rec.title or rec.category == 'News'): #if it's a news show
			outtitle = '{}-{}'.format(outtitle, str(rec.starttime.strftime("%Y%m%d")))
        elif rec.originalairdate != None and rec.originalairdate > datetime.date(datetime(1, 1, 1, 0, 0)): #if it has an original air date
            outtitle = '{} {}'.format(outtitle, str(rec.originalairdate.strftime("%Y%m%d")))
        else:
            outtitle = '{} {}'.format(outtitle, str(rec.starttime.strftime("%Y%m%d")))
        outtitle = '{}.{}'.format(outtitle, filetype)
		
        outfile = os.path.join(sg.dirname, outtitle)
        tmpfile = '{}.{}'.format(outfile.rsplit('.',1)[0], infile.rsplit('.',1)[1])
        if tmpfile == infile:
            tmpfile = '{}.tmp'.format(infile.rsplit('.',1)[0])
        
        if os.path.isfile(outfile):
            outfile = '{}-{}.{}'.format(outfile.rsplit('.',1)[0],str(rec.starttime.strftime("%Y%m%d")),filetype)
        if infile == tmpfile:
			tmpfile = '{}-{}.tmp'.format(outfile.rsplit('.',1)[0],str(rec.starttime.strftime("%Y%m%d")))
        if (overwrite == 0):
			outfile = os.path.join(exportFolder, outtitle)
        if debug:
            print 'infile  "{}"'.format(infile)
            print 'tmpfile "{}"'.format(tmpfile)
            print 'outfile "{}"'.format(outfile)

        clipped_bytes=0;
        # If selected, create a cutlist to remove commercials via mythtranscode by running:
        # mythutil --gencutlist --chanid $CHANID --starttime $STARTTIME
        if generate_commcutlist:
            if jobid:
                job.update({'status':job.RUNNING, 'comment':'Generating Cutlist for commercial removal'})
            task = System(path='mythutil', db=db)
            try:
                output = task('--gencutlist',
                              '--chanid "%s"' % chanid,
                              '--starttime "%s"' % starttime)
            except MythError, e:
                print 'Command "mythutil --gencutlist" failed with output:\n%s' % e.stderr
                if jobid:
                    job.update({'status':job.ERRORED, 'comment':'Generation of commercial Cutlist failed'})
                sys.exit(e.retcode)

        # Lossless transcode to strip cutlist
        if generate_commcutlist or rec.cutlist==1:
            if debug:
                print 'Removing Cutlist'
            if jobid:
                job.update({'status':job.RUNNING, 'comment':'Removing Cutlist'})
            task = System(path='mythtranscode', db=db)
            try:
                output = task('--chanid "%s"' % chanid,
                              '--starttime "%s"' % starttime,
                              '--mpeg2',
                              '--honorcutlist',
                              '-o "%s"' % tmpfile,
                              '1>&2')
                clipped_filesize = os.path.getsize(tmpfile)
                clipped_bytes = input_filesize - clipped_filesize
                clipped_compress_pct = float(clipped_bytes)/input_filesize 
                rec.cutlist = 0
                rec.filesize=clipped_filesize
                rec.update()
            except MythError, e:
                print 'Command "mythtranscode --honorcutlist" failed with output:\n%s' % e.stderr
                if jobid:
                    job.update({'status':job.ERRORED, 'comment':'Removing Cutlist failed. Copying file instead.'})
                copyfile('%s' % infile, '%s' % tmpfile)
                clipped_filesize = input_filesize
                clipped_bytes = 0
                clipped_compress_pct = 0
                pass
        else:
            if debug:
                print 'Creating temporary file for transcoding.'
            if jobid:
                job.update({'status':job.RUNNING, 'comment':'Creating temporary file for transcoding.'})
            copyfile('%s' % infile, '%s' % tmpfile)
            clipped_filesize = input_filesize
            clipped_bytes = 0
            clipped_compress_pct = 0
            
        if infile != '{}.{}'.format(outfile.rsplit('.',1)[0],infile.rsplit('.',1)[1]):
            rec.basename = os.path.basename('{}.{}'.format(outfile.rsplit('.',1)[0], infile.rsplit('.',1)[1]))
            rec.update()

        #TODO : create new function for the work of rendering the file so it can be called twice in the case of an HD and SD dual rendering.
        duration_secs = float(0)
        scaling = "--maxHeight {} --maxWidth {}".format(maxHeight,maxWidth)
        #task = subprocess.Popen('{} --Inform="{};%{}%" "{}"'.format(mediainfo, 'Video', 'Duration', infile), stdout=subprocess.PIPE, shell=True)
        #duration_msecs, e = task.communicate()
        duration_msecs, e = get_mediainfo(db, rec, infile, 'Duration')
        duration_secs = float(int(duration_secs)/1000)
        #duration_secs = float(int(task('--Inform="Video;%%Duration%%" "%s"' % infile))/1000)
        # Estimate bitrate, and detect duration and number of frames
        if estimateBitrate:
            if jobid:
                job.update({'status':job.RUNNING, 'comment':'Estimating bitrate; detecting frames per second, and resolution.'})

            if duration_secs>0:
                bitrate = int(clipped_filesize*8/(1024*duration_secs))
            else:
                print 'Estimate bitrate failed falling back to constant rate factor encoding.\n'
                estimateBitrate = False
                duration_secs = 0

        # get framerate of mpeg2 video stream and detect if stream is HD
        r = re.compile('mpeg2video (.*?) fps,')
        framerate, e = get_mediainfo(db, rec, infile, 'FrameRate')
        vidwidth, e = get_mediainfo(db, rec, infile, 'Width')
        vidheight, e = get_mediainfo(db, rec, infile, 'Height')

        if debug:
            print "Video Dimentions: {}x{}".format(vidwidth, vidheight)
        isHD = False

        if vidwidth > 640 and vidheight > 480 and sdonly < 1:
            if usemkv == 1:
                maxWidth = int(vidwidth)
                maxHeight = int(vidheight)
                scaling = "--maxHeight {} --maxWidth {}".format(maxHeight,maxWidth)
            if debug:
                print 'Stream is HD'
            isHD = True
        elif sdonly > 0:
            isHD = False
            maxWidth = 640
            maxHeight = 360

        if (vidwidth > maxWidth and vidheight > maxHeight):
            scaling = "{} -w {} -l {}".format(scaling,maxWidth,maxHeight)
        else:
            if debug:
                print 'Stream is not HD'
        if debug:
            print "Output Video Dimentions: {}x{}".format(maxWidth, maxHeight)
        if debug:
            print 'Framerate {}'.format(framerate)

        # Setup transcode video bitrate and quality parameters
        # if estimateBitrate is true and the input content is HD:
        #     encode 'medium' preset and vbitrate = inputfile_bitrate*compressionRatio
        # else:
        #     encode at user default preset and constant rate factor ('slow' and 20) 
        preset = preset_nonHD
        if estimateBitrate:
            if isHD:
                h264_bitrate = int(bitrate*compressionRatio)
                # HD coding with specified target bitrate (CRB encoding)
                if hdvideo_tgt_bitrate > 0 and h264_bitrate > hdvideo_tgt_bitrate:
                    h264_bitrate = hdvideo_tgt_bitrate;
                    vbitrate_param = '-b:v %dk' % h264_bitrate
                else:   # HD coding with disabled or acceptable target bitrate (CRF encoding)
                    vbitrate_param = '-crf:v %s' % crf
                preset = preset_HD
            else: # non-HD encoding (CRF encoding)
                vbitrate_param = '-crf:v %s' % crf            
        else:
            vbitrate_param = '-crf:v %s' % crf
        vbitrate_param = '--encopts b-adapt=2:8x8dct=0:cabac=0:'
        if hdvideo_min_bitrate > 0:
            vbitrate_param = vbitrate_param + ':vbv-minrate=%sk' % hdvideo_min_bitrate
        if hdvideo_max_bitrate > 0:
            vbitrate_param = vbitrate_param + ':vbv-maxrate=%sk' % hdvideo_max_bitrate
        if hdvideo_max_bitrate > 0 or hdvideo_min_bitrate > 0:
            vbitrate_param = vbitrate_param + ':vbv-bufsize=%sk' % device_bufsize

        vbitrate_param = vbitrate_param + ':ratetol=inf -q%s ' % crf
        if debug:
            print 'Video bitrate parameter "%s"' % vbitrate_param
            print 'Video h264 preset parameter "%s"' % preset

        # Setup transcode audio bitrate and quality parameters
        # Right now, the setup is as follows:
        # if input is HD: 
        #    copy audio streams to output, i.e., input=output audio
        # else:
        #    output is libfdk_aac encoded at 128kbps 
        if isHD:
            abitrate_param = abitrate_param_HD  # preserve 5.1 audio
        else:
            abitrate_param = abitrate_param_nonHD
        if debug:
            print 'Audio bitrate parameter "%s"' % abitrate_param
        if burncc == 1:
            burnccopt = '--subtitle-burned'
        else:
            burnccopt = ''

        # HandBrakeCLI output is redirected to the temporary file tmpstatusfile and
        # a second thread continuously reads this file while
        # the transcode is in-process. see while loop below for the monitoring thread
        tf = tempfile.NamedTemporaryFile(dir='/media/mythtv1/tmp/hcli-x264/',delete=True)
        tmpstatusfile = tf.name
        if debug:
            print 'Using temporary file "%s" for HandBrakeCLI status updates.' % tmpstatusfile
        res = []
        # create a thread to perform the encode
        ipq = Queue.Queue()
        t = threading.Thread(target=wrapper, args=(encode, 
                            (jobid, db, job, ipq, preset, scaling, burnccopt, usemkv, vbitrate_param, abitrate_param,
                             tmpfile, outfile, tmpstatusfile,), res))
        t.start()
        # wait for HandBrakeCLI to open the file and emit its initialization information 
        # before we start the monitoring process
        time.sleep(1) 
        # open the temporary file having the ffmeg output text and process it to generate status updates
        hangiter=0;
        with open(tmpstatusfile) as f:
            # read all the opening HandBrakeCLI status/analysis lines
            lines = f.readlines()
            # set initial progress to -1
            prev_progress=-1
	    framenum=0
	    fps=1.0
            while t.is_alive():
                # read all output since last readline() call
                lines = f.readlines()
                if len(lines) > 0:
                    # every HandBrakeCLI output status line ends with a carriage return '\r'
                    # split the last read line at these locations
                    lines=lines[-1].split('\r')
                    hangiter=0
                    if len(lines) > 1 and lines[-2].startswith('frame'):
                        # since typical reads will have the last line ending with \r the last status
                        # message is at index=[-2] start processing this line
                        # replace multiple spaces with one space
                        lines[-2] = re.sub(' +',' ',lines[-2])
                        # remove any spaces after equals signs
                        lines[-2] = re.sub('= +','=',lines[-2])
                        # split the fields at the spaces the first two fields for typical
                        # status lines will be framenum=XXXX and fps=YYYY parse the values
                        values = lines[-2].split(' ')
                        if len(values) > 1:
                            if debug:
                                print 'values %s' % values
                            prev_framenum = framenum
                            prev_fps = fps
                            try:
                                # framenum = current frame number being encoded
                                framenum = int(values[0].split('=')[1])
                                # fps = frames per second for the encoder
                                fps = float(values[1].split('=')[1])
                            except ValueError, e:
			        print 'HandBrakeCLI status parse exception: "%s"' % e
                                PrintException()
                                framenum = prev_framenum
                                fps = prev_fps
                                pass
                        # progress = 0-100 represent percent complete for the transcode
                        progress = int((100*framenum)/(duration_secs*framerate))
                        # eta_secs = estimated number of seconds until transcoding is complete
                        eta_secs = int((float(duration_secs*framerate)-framenum)/fps)
                        # pct_realtime = how many real seconds it takes to encode 1 second of video
                        pct_realtime = float(fps/framerate) 
                        if debug:
                            print 'framenum = %d fps = %.2f' % (framenum, fps)                
                        if progress != prev_progress:
                            if debug:
                                print 'Progress %d%% encoding %.1f frames per second ETA %d mins' \
                                      % ( progress, fps, float(eta_secs)/60)
                            if jobid:
                                progress_str = 'Transcoding to % %d%% complete ETA %d mins fps=%.1f.' \
                                      % ( filetype, progress, float(eta_secs)/60, fps)
                                job.update({'status':job.RUNNING, 'comment': progress_str})
                            prev_progress = progress
                    elif len(lines) > 1:
                        if debug:
                            print 'Read pathological output %s' % lines[-2]
                        if jobid:
                            progress_str = 'Read pathological output %s' % lines[-2]
                            job.update({'status':job.RUNNING, 'comment': progress_str})
                else:
                    if debug:
                        print 'Read no lines of HandBrakeCLI output for %s secs. Possible hang?' % (POLL_INTERVAL*hangiter)
                    hangiter = hangiter + 1
                    if jobid:
                        progress_str = 'Read no lines of HandBrakeCLI output for %s secs. Possible hang?' % (POLL_INTERVAL*hangiter)
                        job.update({'status':job.RUNNING, 'comment': progress_str})
                time.sleep(POLL_INTERVAL)
            if debug:
                print 'res = "%s"' % res

        t.join(1)
        try:
            if ipq.get_nowait() == CleanExit:
                sys.exit()
        except Queue.Empty:
            pass

        if flush_commskip:
            task = System(path='mythutil')
            task.command('--chanid %s' % chanid,
                         '--starttime %s' % starttime,
                         '--clearcutlist',
                         '2> /dev/null')
            task = System(path='mythutil')
            task.command('--chanid %s' % chanid,
                         '--starttime %s' % starttime,
                         '--clearskiplist',
                         '2> /dev/null')

        if flush_commskip:
            for index,mark in reversed(list(enumerate(rec.markup))):
                if mark.type in (rec.markup.MARK_COMM_START, rec.markup.MARK_COMM_END):
                    del rec.markup[index]
            rec.bookmark = 0
            rec.cutlist = 0
            rec.markup.commit()

        try:
            os.remove('%s.map' % tmpfile)
        except OSError:
            pass
        
        # if we are replacing the original file
        if overwrite == 1:
            rec.basename = os.path.basename(outfile)
            rec.filesize = os.path.getsize(outfile)
            rec.transcoded = 1
            rec.seek.clean()
            rec.update()
        #else: #TODO: need to figure out how to create a new record for the version with CC burned in.
			#rec.starttime = rec.starttime + timedelta(minutes=1)
            #rec.endtime = rec.endtime + timedelta(minutes=1)
            #rec._create_normal()
            #rec = Recorded((chanid, utcstarttime + timedelta(minutes=1)), db=db);
        # ENDIF overwrite = 1

        # Add Metadata to outfile based on the data in the rec object
        add_metadata(db, jobid, debug, job, rec, filetype, outfile)
		
        #replace the original infile with the tmp file, so remove the infile
        if infile != '{}.{}'.format(os.path.join(sg.dirname, outtitle.rsplit('.',1)[0]), infile.rsplit('.',1)[1]) or overwrite == 1:
            os.remove(infile)
            print 'delete infile: {}.{}'.format(os.path.join(sg.dirname, outtitle.rsplit('.',1)[0]), infile.rsplit('.',1)[1])
        if infile == '{}.{}'.format(os.path.join(sg.dirname, outtitle.rsplit('.',1)[0]), infile.rsplit('.',1)[1]) or overwrite == 1:
            os.remove(tmpfile)
            print 'delete tmp: {}'.format(tmpfile)
        	
        # Cleanup the old *.png files
        for filename in glob('%s*.png' % infile):
            os.remove(filename)
        #

        output_filesize = rec.filesize
        if duration_secs > 0:
            output_bitrate = int(output_filesize*8/(1024*duration_secs)) # kbps
        actual_compression_ratio = 1 - float(output_filesize)/clipped_filesize
        compressed_pct = 1 - float(output_filesize)/input_filesize

        if build_seektable:
            if jobid:
                job.update({'status':job.RUNNING, 'comment':'Rebuilding seektable'})
            if debug:
                print 'Rebuilding seektable'
            task = System(path='mythcommflag')
            task.command('--chanid %s' % chanid,
                         '--starttime %s' % starttime,
                         '--rebuild',
                         '2> /dev/null')

        if overwrite == 1:
            # fix during in the recorded markup table this will be off if commercials are removed
            duration_msecs, e = get_mediainfo(db, rec, outfile, 'Duration')
            for index,mark in reversed(list(enumerate(rec.markup))):
                # find the duration markup entry and correct any error in the video duration that might be there
                if mark.type == 33:
                    if debug:
                        print 'Markup Duration in milliseconds "{}"'.format(mark.data)
                        print 'Duration_msecs = {}'.format(duration_msecs)
                    error = int(mark.data) - int(duration_msecs)
                    if error != 0:
                        if debug:
                            print 'Markup Duration error is "%s"msecs' % error
                        mark.data = duration_msecs
                        #rec.bookmark = 0
                        #rec.cutlist = 0
                        rec.markup.commit()

        if jobid:
            if output_bitrate:
                job.update({'status':job.FINISHED, 'comment':'Transcode Completed @ %dkbps, compressed file by %d%% (clipped %d%%, transcoder compressed %d%%)' % (output_bitrate,int(compressed_pct*100),int(clipped_compress_pct*100),int(actual_compression_ratio*100))})
            else:
                job.update({'status':job.FINISHED, 'comment':'Transcode Completed'})
    except:
        print 'Error when executing the transcode. Job aborted.'
        if debug:
               PrintException()
        if jobid:
            job.update({'status':job.ERRORED, 'comment':'Error when executing the transcode. Job aborted.'})
        sys.exit()

# DEFINE function to store metadata for the outfile.
def add_metadata(db=None, jobid=None, debug=False, job=None, rec=None, filetype='mkv', filename=None, metaapp='/usr/bin/AtomicParsley'):
    if debug:
        print 'Adding metadata to the file.'
    if jobid:
        progress_str = 'Adding metadata to the file.'
        job.update({'status':job.RUNNING, 'comment': progress_str})
    # Use AtomicParsley for metadata work on the file if it's not a MKV
    if filetype != 'mkv':
        tmptitle = rec.title.encode('utf-8').strip()
        if rec.programid[0:2] == 'MV':
            # TODO: Add Actors and Director - need to loop over rec.cast object and reference rec.cast[x].role to determine if actor/director
            tmptitle = '{}'.format(rec.title.encode('utf-8').strip())
        if rec.season > 0 and rec.episode > 0:
            tmptitle = '{0:s} S{1:d} E{2:02d}'.format(rec.title.encode('utf-8').strip(), rec.season, rec.episode)

        atomicparams = '"{}" --title "{}" --genre "{}" --year "{}" --TVShowName "{}" --TVSeasonNum "{}" --TVEpisodeNum "{}" --TVEpisode "{}" --comment "{}" --description "{}" --longdesc "{}" --overWrite'.format(os.path.realpath(filename), tmptitle, rec.category, rec.originalairdate, rec.title.encode('utf-8').strip(), rec.season, rec.episode, rec.programid, rec.subtitle.encode('utf-8').strip(), rec.subtitle.encode('utf-8').strip(), rec.description.encode('utf-8').strip())

        try:
            metatask = System(path='nice', db=db)
            metatask('-n {} {} {}'.format(NICELEVEL, metaapp, atomicparams))
        except Exception as e:
            if debug:
                PrintException()
                print 'Adding metadata to the filename failed. Run this manually: /usr/bin/AtomicParsley {}'.format(atomicparams)
            if jobid:
                job.update({'status':job.FINISHED, 'comment':'Adding metadata to the filename failed. Run this manually: /usr/bin/AtomicParsley {}'.format(atomicparams)})

def get_mediainfo(db=None, rec=None, filename=None, infosubtype='Duration', infotype='Video', transcoder='/usr/bin/mediainfo'):
    task = subprocess.Popen('{} --Inform="{};%{}%" "{}"'.format(transcoder, infotype, infosubtype, filename), stdout=subprocess.PIPE, shell=True)
    duration_msecs = 0
    if filename is None:
        return -1
    
    try:
        duration_msecs, e = task.communicate()
    except MythError, e:
        pass
    
    if duration_msecs.strip() > 0:
        #duration_secs = float(duration_msecs/1000)
        #if debug:
            #print 'Duration in seconds "%s"' % duration_secs
            #print 'Duration in milliseconds "%s"' % duration_msecs
        return duration_msecs.strip(), e
    return -1, e

def encode(jobid=None, db=None, job=None, 
           procqueue=None, preset='slow', scaling='', burncc='', usemkv=0,
           vbitrate_param='-q 21',
           abitrate_param='--aencoder copy:ac3',
           tmpfile=None, outfile=None, statusfile=None):
#    task = System(path=transcoder, db=db)
    task = System(path='nice', db=db)
    if debug:
        script = 'nice -n {} {} -i "{}"'.format(NICELEVEL, transcoder, tmpfile)
        # parameter to allow streaming content
        script = '{} -O {} --markers --detelecine --strict-anamorphic'.format(script, scaling)
        # h264 video codec
        script = '{} --large-file --encoder x264'.format(script)
        if usemkv == 1:
            # use the Normal preset for MKV
            script = '{} -Z Normal'.format(script)
            # parameter to copy input subtitle streams into the output
            script = '{} -s 1'.format(script)
        else:
            # presets for h264 encode that effect encode speed/output filesize
            script = '{} --encopts {}'.format(script, preset)
            # parameters to determine video encode target bitrate
            script = '{} {}'.format(script, vbitrate_param)
            # parameters to determine audio encode target bitrate
            script = '{} {}'.format(script, abitrate_param)
            # parameter to copy input subtitle streams into the output
            script = '{} -s 1 {}'.format(script, burncc)
        # output file parameter
        script = '{} -o "{}"'.format(script, outfile)
        # redirection of output to temporaryfile
        script = '{} > {} 2>&1 < /dev/null'.format(script, statusfile)
        print 'Executing Transcoder: \n{}'.format(script)
    try:
        output = task('{}'.format(script))

    except MythError, e:
        PrintException()
        #print 'Command failed with output:\n{}'.format(e.ename,e.args,e.stderr)
        #TODO: if failure, need to return a failure code to abort the rest of the script.
        if jobid:
            job.update({'status':job.ERRORED, 'comment':'Transcoding to {} failed'.format(filetype)})
        procqueue.put(CleanExit)
        sys.exit(e.retcode)

def PrintException():
    exc_type, exc_obj, tb = sys.exc_info()
    f = tb.tb_frame
    lineno = tb.tb_lineno
    filename = f.f_code.co_filename
    linecache.checkcache(filename)
    line = linecache.getline(filename, lineno, f.f_globals)
    print 'EXCEPTION IN ({}, LINE {} "{}"): {}'.format(filename, lineno, line.strip(), exc_obj)

def wrapper(func, args, res):
    res.append(func(*args))

def main():
    parser = OptionParser(usage="usage: %prog [options] [jobid]")

    parser.add_option('--chanid', action='store', type='int', dest='chanid',
            help='Use chanid with both starttime and tzoffset for manual operation')
    parser.add_option('--starttime', action='store', type='string', dest='starttime',
            help='Use starttime with both chanid and tzoffset for manual operation')
    parser.add_option('--tzoffset', action='store', type='int', dest='tzoffset',
            help='Use tzoffset with both chanid and starttime for manual operation')
    parser.add_option('--overwrite', action='store', type='int', default=1, dest='overwrite',
            help='Use overwrite to force the output to replace the original file')
    parser.add_option('--sd', action='store', type='int', default=0, dest='sdonly',
            help='Use sd to force the output to be SD dimentions')
    parser.add_option('--burncc', action='store', type='int', default=0, dest='burncc',
            help='Use burncc to burn the subtitle/cc into the video')
    parser.add_option('--mkv', action='store', type='int', default=0, dest='usemkv',
            help='Use mkv instead of mp4')
    parser.add_option('-v', '--verbose', action='store', type='string', dest='verbose',
            help='Verbosity level')

    opts, args = parser.parse_args()

    if opts.verbose:
        if opts.verbose == 'help':
            print MythLog.helptext
            sys.exit(0)
        MythLog._setlevel(opts.verbose)

    if len(args) == 1:
        runjob(jobid=args[0], overwrite=opts.overwrite, sdonly=opts.sdonly, burncc=opts.burncc, usemkv=opts.usemkv)
    elif opts.chanid and opts.starttime and opts.tzoffset is not None:
        runjob(chanid=opts.chanid, starttime=opts.starttime, tzoffset=opts.tzoffset, overwrite=opts.overwrite, sdonly=opts.sdonly, burncc=opts.burncc, usemkv=opts.usemkv)
    else:
        print 'Script must be provided jobid, or chanid, starttime and timezone offset.'
        sys.exit(1)

if __name__ == '__main__':
    main()
