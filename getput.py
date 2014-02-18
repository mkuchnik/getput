#!/usr/bin/python -u

# Copyright 2013 Hewlett-Packard Development Company, L.P.
# Use of this script is subject to HP Terms of Use at
# http://www8.hp.com/us/en/privacy/terms-of-use.html.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#    http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# WEIRDNESS
# - 1419 PUTs are slower than 1420 bytes puts because of nagel
# - 7887 byte GETs faster than 7888->~16K byt gets, re: pound/nagel
# - client side PUTs of 1419 bytes slower than 1420 byte puts, re: nagel
# - sync > 10 secs after conn & before 1st oper causes a 1-sec latency
#   overcome by forcing communications every 8 seconds during waiting period
# - sync > 10 AND --nocomp causes lower level EOF error in ssl code, which also
#   overcome by same mechanism as above

# debug
#   1 - print some basic stuff. inclucing test starts info
#   2 - show cont/obj names on calls
#   4 - report container header info
#   8 - show name of container being used
#  16 - show inputs for starting multiprocessing
#  32 - show trandIDs/latencies, only make sense for small values of -n
#  64 - show connection information
# 128 - trace execution to log in /tmp

# logops masks
#   1 - log all latencies
#   2 - log traces [type 3 calls]
#   4 - log latencies > :latency

import sys
import os
import re
import struct
import random
import signal
import socket
import string
import time
import inspect
import cStringIO
from random import randint
from optparse import OptionParser, OptionGroup
from multiprocessing import Pool
from urllib import quote
from swiftclient import Connection
from swiftclient import ClientException
from swiftclient import put_object


# Handy for tracing execution of getput itself
def logexec(text):

    if debug & 128:
        logfile = '/tmp/getput-exec-%s.log' % (time.strftime('%Y%m%d'))
        log = open(logfile, 'a')
        log.write('%s %s\n' % (time.strftime('%H:%M:%S', time.gmtime()), text))
        log.close()


def error(text, exit_flag=True):
    """
    Main error reporting, usually exits
    """

    print "Error -- Host: %s getput: %s" % (socket.gethostname(), text)
    if exit_flag:
        sys.exit(0)


def reset_last(num_procs):
    """
    Sets the ending object numbers for each process to get, put or delete

    some explanation needed...  last[] contains the max number of objects to
    PUT, GET oe DELETE per process.  In the case of multiple sets of tests,
    these need to be reset to the original value specified with -n and if a
    single value spread across all procs. Since a PUT test also resets last[]
    to contain actual number of objects so GET/DEL will need to know how many
    there are.  The errors can only happen looking at original values of -n.
    """

    global last

    last = []
    if options.nobjects and re.search(':', options.nobjects):
        for string in options.nobjects.split(':'):
            try:
                last.append(int(string))
            except ValueError:
                error('-n must be a set of : separated integers')
    else:
        for i in range(num_procs):
            # reset everything to what was specified with -n and if not there
            # we already know there's a runtime so set a huge max objects
            if options.nobjects:
                numobj = int(options.nobjects)
            else:
                numobj = 999999

            try:
                last.append(numobj)
            except ValueError:
                error('-n must be an integer')


def getenv(varname):
    """
    get value for environment variable
    """

    try:
        value = os.environ[varname]
    except KeyError:
        value = ''
    return(value)


def parse_creds():
    """
    parse credentials either from environment OR credentials file
    """

    # remember, --creds overrides environment
    stnum = 0
    stvars = {}
    for varname in ['ST_AUTH', 'ST_USER', 'ST_KEY']:
        stvars[varname] = getenv(varname)
        if stvars[varname] != '':
            stnum += 1

    osnum = 0
    osvars = {}
    for varname in ['OS_AUTH_URL', 'OS_USERNAME', 'OS_PASSWORD', \
                        'OS_TENANT_ID', 'OS_TENANT_NAME']:
        osvars[varname] = getenv(varname)
        if osvars[varname] != '':
            osnum += 1

    if stnum > 0 and  osnum > 0:
        error('you have both ST_ and OS_style varibles defined' + \
                  ' in your environment and you must only have 1 type')
    if stnum > 0 and stnum != 3:
        error('you have at least 1 ST_ style variable defined but not all 3')
    if osnum > 0:
        if osvars['OS_AUTH_URL'] == '' or osvars['OS_USERNAME'] == '' \
                                       or osvars['OS_PASSWORD'] == '':
            error('your environment has at least 1 OS_ style variable ' + \
                  'defined but not OS_AUTH_URL, OS_USERNAME or OS_PASSWORD')
        if osvars['OS_TENANT_NAME'] == '' and osvars['OS_TENANT_ID'] == '':
            error('your environment has at least 1 OS_ style variable ' + \
                  'defined but not OS_TENANT_NAME or OS_TENANT_ID')

    username = password = endpoint = tenant_id = tenant_name = ''
    if stnum > 1:
        endpoint = stvars['ST_AUTH']
        username = stvars['ST_USER']
        password = stvars['ST_KEY']
        tenant_id = tenant_name = ''
    elif osnum > 1:
        endpoint = osvars['OS_AUTH_URL']
        username = osvars['OS_USERNAME']
        password = osvars['OS_PASSWORD']
        tenant_id = osvars['OS_TENANT_ID']
        tenant_name = osvars['OS_TENANT_NAME']

    return((endpoint, username, password, tenant_id, tenant_name))


def main(argv):
    """
    read/parse switches
    """

    global debug, compress, options, procset, sizeset, ldist10
    global username, password, endpoint, tenant_id, tenant_name, errmax
    global latexc_min, latexc_max

    ldist10 = 0
    procset = [1]
    latexc_min = latexc_max = 9999

    parser = OptionParser(add_help_option=False)
    group0 = OptionGroup(parser, 'these are the basic switches')
    group0.add_option('-c', '--cname',    dest='cname',
                      help='container name')
    group0.add_option('-d', '--debug',    dest='debug',
                      help='debugging mask', default=0)
    group0.add_option('-n', '--nobjects', dest='nobjects',
                      help='containter/object numbers as a value OR range')
    group0.add_option('-o', '--obj',      dest='oname',
                      help='object name prefix')
    group0.add_option('-r', '--runtime',  dest='runtime',
                      help="runtime in secs")
    group0.add_option('-s', '--size',     dest='sizeset',
                      help='object size(s)')
    group0.add_option('-t', '--tests',    dest='tests',
                      help='tests to run [gpd]')
    group0.add_option('-h', '--help', dest='help',
                      help='show this help message and exit',
                      action='store_true')
    group0.add_option('-v', '--version',  dest='version',
                      help='print version and exit',
                      action='store_true')
    parser.add_option_group(group0)

    groupa = OptionGroup(parser, 'these switches control the output')
    groupa.add_option('--ldist',    dest='ldist',
                      help="report latency distributions at this granularity")
    groupa.add_option('--nohead',   dest='nohead',
                      help="do not print header with results",
                      action='store_true', default=False)
    groupa.add_option('--psum',     dest='psum',
                      help="include process summary in output",
                      action='store_true', default=False)
    groupa.add_option('--putsperproc', dest='putsperproc',
                      action='store_true', default=False,
                      help='list numbers of puts by each process')
    parser.add_option_group(groupa)

    groupc = OptionGroup(parser, 'these switches effect behavior')
    groupc.add_option('--cont-nodelete', dest='cont_nodelete',
                      help="do not delete container after a delete test",
                      action='store_true', default=False)
    groupc.add_option('--ctype',    dest='ctype', default='shared',
                      help="container type: shared|bynode|byproc, def=byproc")
    groupc.add_option('--errmax',   dest='errmax',
                      help="quit after this number of errors, [def=5]",
                      default=5)
    groupc.add_option('--latexc',   dest='latexc',
                      help="stop when max latency matches exception")
    groupc.add_option('--logops',   dest='logops',
                      help="log latencies for all operations",
                      default='0')
    groupc.add_option('--nocompress', dest='nocompress',
                      help="disable ssl compression", action='store_true',
                      default=False)
    groupc.add_option('--objopts',    dest='objopts', default='',
                      help='object options [acfu]')
    groupc.add_option('--preauthtoken', dest='preauthtoken',
                      default='',
                      help="use this preauthtoken with --proxies")
    groupc.add_option('--procs',    dest='procset',
                      help="number of processes to run")
    groupc.add_option('--proxies',   dest='proxies', default='',
                      help='bypass load balancer and connect directly')
    groupc.add_option('--quiet',   dest='quiet', default=False,
                      help='suppress api errors & sync time warnings',
                      action='store_true')
    groupc.add_option('--repeat',   dest='repeats',
                      help='number of time to repeat --num tests')
    groupc.add_option('--warnexit', dest='warnexit',
                      help='exit on warnings', action='store_true')
    parser.add_option_group(groupc)

    groupb = OptionGroup(parser, 'multi-node access')
    groupb.add_option('--creds',    dest='creds',
                      help='credentials')
    groupb.add_option('--rank',     dest='rank',
                      help='rank among clients, used in obj/container names',
                      default='0')
    groupb.add_option('--sync',     dest='synctime',
                      help='time, in seconds since epoch, to start test')
    groupb.add_option('--utc',     dest='utc',
                      action='store_true', default=False,
                      help='append utc time to container names')
    parser.add_option_group(groupb)

    try:
        (options, args) = parser.parse_args(argv)
    except:
        print 'invalid command'
        sys.exit()

    if options.help:
        parser.print_help()
        sys.exit()

    if options.version:
        print 'getput V%s\n\n%s' % (version, copyright)
        sys.exit()

    #    T h e s e    h a v e    d e f a u l t s

    (endpoint, username, password, tenant_id, tenant_name) = parse_creds()

    try:
        debug = int(options.debug)
    except:
        error('-d must be an integer')

    try:
        errmax = int(options.errmax)
    except ValueError:
        error('--errmax must be an integer')

    #    G e t    C r e d e n t i a l s    f r o m    C r e d s    F i l e

    if options.creds:
        creds = options.creds
        try:
            f = open(creds, 'r')
            user = password = endpoint = ''
            for line in f:
                if re.match('\#|\s*$', line):
                    continue
                line = line.rstrip('\n')

                search = re.search('OS_AUTH_URL=(.*)', line) or \
                         re.search('ST_AUTH=(.*)', line)
                if search:
                    endpoint = search.group(1)
                    endpoint = endpoint.strip(";'\"")

                search = re.search('OS_USERNAME=(.*)', line) or \
                         re.search('ST_USER=(.*)', line)
                if search:
                    username = search.group(1)
                    username = username.strip(";'\"")

                search = re.search('OS_PASSWORD=(.*)', line) or \
                         re.search('ST_KEY=(.*)', line)
                if search:
                    password = search.group(1)
                    password = password.strip(";'\"")

                search = re.search('OS_TENANT_ID=(.*)', line)
                if search:
                    tenant_id = search.group(1)
                    tenant_id = tenant_id.strip(";'\"")

                search = re.search('OS_TENANT_NAME=(.*)', line)
                if search:
                    tenant_name = search.group(1)
                    tenant_name = tenant_name.strip(";'\"")

        except IOError, err:
            error("Couldn't read creds file: " + creds)

    #     R e q u i r e d :   - - t e s t s    &    - - c n a m e

    if username == '' or password == '' or endpoint == '':
        error('specify credentials with --creds OR set ST_* variables')

    if options.tests:
        if not (re.match('[,pgdPGD]+\Z', options.tests)):
            error("valid tests are comma separated combinations of: gpd")
    else:
        error('define test list with -t')

    # cname and oname actually defined in init_test()
    if not options.cname:
        error('specify container name with -c')

    if options.objopts:
        if not re.match('[acfu]', options.objopts):
            error("--objopts must be a combination of 'acfu'")

    #    T e s t    D e p e n d e n t    S w i t c h e s

    if re.match('[gpd]', options.tests):
        if not options.oname:
            error('get, put and delete tests require object name')

    if options.sizeset:
        if re.search(',', options.sizeset):
            if not re.match('p', options.tests):
                error('multiple obj sizes require PUT test')

        sizeset = []
        for size in options.sizeset.split(','):
            match = re.match('(\d+)([kmg]?\Z)', size, re.I)
            if (match):
                sizeset.append(size)
            else:
                error('object size must be a number OR number + k/m/g')
    else:
        error('object size required')

    #    O p t i o n a l

    if options.procset:
        procset = []
        for proc in options.procset.split(','):
            try:
                procset.append(int(proc))
            except ValueError:
                error('--procs must be an integer')

    if options.ldist:
        try:
            if int(options.ldist) > 3:
                error("--ldist > 3 not supported")
        except ValueError:
            error('--ldist must be an integer')
        ldist10 = 10 ** int(options.ldist)

    if options.runtime:
        if not re.match('\d+$', options.runtime):
            error('--runtime must be an integer')

    if options.ctype != None:
        if not re.match('shared|bynode|byproc', options.ctype):
            error("invalid ctype, expecting: 'shared|bynode|byproc'")

    if options.rank and not re.match('\d+$', options.rank):
        error('--rank must be an integer')

    # initialze last[] for all processes based on first value of -n
    if options.nobjects:
        reset_last(procset[0])
    elif not options.runtime:
        error('specify at least one of -n and/or --runtime')

    if options.repeats and not re.match('\d+$', options.repeats):
        error('-r must be an integer')

    if options.synctime and not re.match('\d+$', options.synctime):
        error('sync time must be an integer')

    if args:
        print "Extra command argument(s):", args
        sys.exit()

    if options.latexc:
        if re.search('-', options.latexc):
            latexc_min, latexc_max = options.latexc.split('-')
        else:
            latexc_min = options.latexc
            latexc_max = 9999

        latexc_min = float(latexc_min)
        latexc_max = float(latexc_max)


def cvtFromKMG(str):
    """
    converts a string containing K, M or G to its equivilent number
    """

    # remember, we already verify sizeset[]
    match = re.match('(\d+)([kmg]?\Z)', str, re.I)
    size = int(match.group(1))
    type = match.group(2).lower()
    if type == '':
        objsize = size
    if type == 'k':
        objsize = size * 1024
    elif type == 'm':
        objsize = size * 1024 * 1024
    elif type == 'g':
        objsize = size * 1024 * 1024 * 1024
    return(objsize)


def cvt2KMG(num):
    """
    converts a string which is a multiple of 1024 to the form: number[KMG]
    """

    # only do this is exact multiple of 1024
    temp = num
    suffix = ''
    if (int(num / 1024) * 1024 == num):
        modifiers = 'kmg'
        while(temp > 1023):
            temp = temp / 1024
            suffix = modifiers[0]
            modifiers = modifiers[1:]
    return(str(temp) + suffix)


native_close = True
if not hasattr(Connection, 'close'):
    native_close = False
    class MyConnection(Connection):

        def close(self):
            if self.http_conn:
                self.http_conn[1].close()


def connect(endpoint, username, password, tenant_id, tenant_name, \
                preauthurl=None, preauthtoken=None):
    """
    make a connection to swift
    """

    global compress

    if re.search('v1.0', endpoint):
        auth_version = '1.0'
    elif re.search('v2.0', endpoint):
        auth_version = '2.0'
    elif re.search('v3.0', endpoint):
        auth_version = '3.0'

    opts = {}
    if tenant_id != '':
        opts['tenant_id'] = tenant_id
        opts['tenant_name'] = tenant_name

    if options.nocompress:
        comp = False
    else:
        comp = True

    if debug & 64:
        print "Connect - User %s Key: %s Options: %s SSL: %s" % \
            (username, password, opts, comp)
        print "Connect - AuthVer: %s AuthURL: %s" % (auth_version, endpoint)
        print "Connect - PreauthURL: %s  PreauthToken: %s" % \
            (preauthurl, preauthtoken)

    # get the connection object
    if preauthurl:
        logexec('connect - PreauthURL: %s  PreauthToken: %s' % \
                    (preauthurl, preauthtoken))
    try:
        response = {}
        if native_close:
            connection = \
                Connection(authurl=endpoint,
                             user=username,
                             key=password,
                             auth_version=auth_version,
                             preauthurl=preauthurl,
                             preauthtoken=preauthtoken,
                             os_options=opts, ssl_compression=comp)
        else:
            connection = \
                MyConnection(authurl=endpoint,
                             user=username,
                             key=password,
                             auth_version=auth_version,
                             preauthurl=preauthurl,
                             preauthtoken=preauthtoken,
                             os_options=opts, ssl_compression=comp)
    except Exception as err:
        import traceback
        print "Connect failure: %s", err
        logexec('connect() exception: %s %s' % (err, traceback.format_exc()))
        return(-1)

    # Just created the connection object, so make sure we're really connected
    # Errors are very rare, but if something misconfigured we want to know!
    logexec('connected')

    try:
        headers = connection.head_account()
    except Exception as err:
        import traceback
        print "head_account failure: %s", err
        logexec('head_account() exception: %s %s' % \
                    (err, traceback.format_exc()))
        return(-1)

    if debug & 4:
        print "Headers: ", headers
    container_count = int(headers.get('x-account-container-count', 0))

    if debug & 64:
        print "connected!", connection

    return(connection)


def logger(optype=None, data=None, inst=None, test_time=None):
    """
    write operation details to a log file, including start/stop times
    and latencies
    types:
        1 - open log
        2 - latency record
        3 - tracing record
        4 - errors
        9 - close logfile

    mask
        1 - just latencies
        2 - just traces
        4 - exception traces
    """

    global logfiles

    if not logmask:
        return

    if optype == 9:
        logfiles[inst].close()
        return()

    # should we log?
    if optype == 2 and (not logmask & 5) or (optype == 3 and not logmask & 2):
        return()

    if optype == 1:
        # data is actually the name of the test for type 1 call
        filename = '/tmp/getput-%s-%d-%d.log' % (data, inst, int(test_time))
        logfiles[inst] = open(filename, 'w')
    else:
        secs = time.time()
        usecs = '%.3f' % (secs - int(secs))
        now = "%s.%s" % (time.ctime(secs).split()[3], usecs.split('.')[1])
        logfiles[inst].write('%s %f %s\n' % (now, secs, data))
        logfiles[inst].flush()


def api_error(type, instance, cname, oname, err):
    """
    Report SWIFT api errors
    """

    time_now = time.strftime('%H:%M:%S')
    error_string = '%s %s %s/%s apierror %d on instance: %d' % \
        (time_now, type, cname, oname, err.http_status, instance)
    if not options.quiet:
        print error_string
    logger(4, 'ApiError: %s ' % err.http_status, instance)


def latcalc(latency, min, max, tot, dist):
    """
    track total latency times and also incrememnt appropriate histogram bucket
    """

    tot = tot + latency
    if latency < min:
        min = latency
    if latency > max:
        max = latency

    # Distribution:  0  1  2  3  4  5 10 20 30 40 50
    # bucket 5 contains values of 5.xxx whereas higher numbered bucket
    # contains values not including that value so bucket 6 goes up to 9.999
    # and 7 up to 19.999
    bucket = int(latency * ldist10)
    if bucket >= 5:
        bucket = int(bucket / 10) + 5
    if bucket > 10:
        bucket = 10
    dist[bucket] = dist[bucket] + 1

    return(min, max, tot)

#########################
#    Object Operations
#########################


def get_offset(procs, instance, csize):

    """
    when doing random I/O, the object numbering depends on container type
    and doing it here makes sure consistent for ALL types of operations
    """

    procs = int(procs)
    numobjs = int(options.nobjects)
    if options.ctype == 'shared':
        offset = numobjs * procs * int(options.rank) + numobjs * instance
    elif options.ctype == 'bynode':
        offset = numobjs * instance
    else:
        offset = 0

    if re.search('a', options.objopts):
        offset += csize

    return(offset)


def put(connection, instance, donetime, cname, csize, oname, random_flag):
    """
    perform PUT operations for 1 process until end time OR requested
    number of operations is reached
    """

    lat_dist = []
    for i in range(11):
        lat_dist.append(0)

    # for stats
    latencies = []

    # we need the cpu counters for when this operation actually starts
    scpu = read_stat()

    t0 = time.time()

    min = 9999
    ops = max = tot = errs = 0
    puts = 1
    maxputs = last[instance]

    # flat hierarchies are based on rank/proc/instance AND if container
    # already exists it will be appended to
    if re.search('f', options.objopts):
        offset = get_offset(procs, instance, csize)

    logexec('call logger')
    logger(3, 'cname: %s  oname: %s  puts: %d  now: %d  done: %d' % \
               (cname, oname, maxputs, time.time(), donetime), instance)

    fp = cStringIO.StringIO(fixed_object)
    while (puts <= maxputs and time.time() < donetime and errs < errmax):

        # build object name based on object type and number
        if random_flag:
            object_number = randint(1, csize)
        elif re.search('f', options.objopts):
            object_number = offset + puts
        else:
            object_number = puts
        objname = '%s-%d' % (oname, object_number)

        if debug & 2:
            print "%s PUT CName: %s  OName: %s Inst: %d" % \
                (time.strftime('%H:%M:%S'), cname, objname, instance)

        puts = puts + 1
        t1 = time.time()
        fp.seek(0)
        try:
            response = {}
            logexec("Call PUT")
            connection.put_object(cname, objname, fp, osize,
                                  response_dict=response)
            logexec("PUT succeeded")
            transID = response['headers']['x-trans-id']

        except ClientException, err:
            api_error('put', instance, cname, objname, err)
            if err.http_status:
                errs = errs + 1
                continue

        # this has been a lot of pain, but if we do get a traceback this is the
        # best way I could think of to both record it locally in the exec.log
        # and pass it back to gpmulti if called that way.
        except Exception as err:
            import traceback
            logexec('put_object() exception: %s %s' % \
                        (err, traceback.format_exc()))
            logger(9, '', instance)
            return(['Unexpected Error - put_object() exception: %s' % \
                        err, instance, 0, 0, 0, 0, 0, 0,
                        lat_dist, latencies, scpu])

        ops = ops + 1
        t2 = time.time()
        latency = t2 - t1
        min, max, tot = latcalc(latency, min, max, tot, lat_dist)
        latencies.append(latency)

        if debug & 32:
            print "%f  %f  TransID: %s Latency: %9.6f  %s/%s" % \
                (t1, t2, transID, latency, cname, objname)

        # note the size if the latecy can vary by object size
        if logmask & 1 or (logmask & 4 and latency > sizelat[size_index]):
            logger(2, "%f  %f  %s  %s/%s" %\
                       (t1, latency, transID, cname, objname), instance)

        # let it continue so all the cleanup stuff find objects to delete
        if latency >= latexc_min and latency <= latexc_max:
            start = time.strftime('%Y%m%d %H:%M:%S', time.gmtime(t1))
            print "Host: %s -- Warning: %s PUT latency exception: %6.3f " \
                "secs ObjSize: %4s TransID: %s Obj: %s/%s" % \
                (socket.gethostname(), start, latency, size, transID,
                 cname, objname)
            if options.warnexit:
                break

    elapsed = time.time() - t0
    logger(3, 'Done!  time: %f ops: %d errs: %d' % \
               (elapsed, ops, errs), instance)
    logger(9, '', instance)

    return(['put', instance, elapsed, ops, min, max, \
                tot, errs, lat_dist, latencies, scpu])


def get(connection, instance, donetime, cname, csize, oname, random_flag):
    """
    perform GET operations for 1 process until end time OR requested
    number of operations is reached
    """

    lat_dist = []
    for i in range(11):
        lat_dist.append(0)

    # for stats
    latencies = []

    # we need the cpu counters for when this operation actually starts
    scpu = read_stat()

    t0 = time.time()

    # NOTE - we need to init objsize just in case we don't get any!
    min = 9999
    ops = max = tot = errs = objsize = 0
    gets = 1
    maxgets = last[instance]

    logger(3, 'cname: %s  oname: %s  gets: %d  now: %d  done: %d' %
           (cname, oname, maxgets, time.time(), donetime), instance)

    while (gets <= maxgets and time.time() < donetime and errs < errmax):

        objsize = 0

        # build object name based on object type and number
        # noting for now, sequential access for flat hierachies
        # is disallowed and only here as a placeholder
        if random_flag:
            object_number = randint(1, csize)
        elif re.search('f', options.objopts):
            object_number = offset + gets
        else:
            object_number = gets
        objname = '%s-%d' % (oname, object_number)

        if debug & 2:
            print "%s GET CName: %s  OName: %s Inst: %d" % \
                (time.strftime('%H:%M:%S'), cname, objname, instance)

        gets = gets + 1
        t1 = time.time()
        try:
            body = []
            headers, body = connection.get_object(cname, objname,
                                                  resp_chunk_size=65536)

            transID = headers['x-trans-id']   # not getting from response_dict

        except ClientException, err:
            api_error('get', instance, cname, objname, err)
            errs = errs + 1
            continue

        except Exception as err:
            import traceback
            logexec('get_object() exception: %s %s' %\
                        (err, traceback.format_exc()))
            logger(9, '', instance)
            return(['Unexpected Error - get_object() exception: %s' \
                        % err, instance, 0, 0, 0, 0, 0, 0, \
                        lat_dist, latencies, scpu])

        # continue reading until we have whole object
        for chunk in body:
            if len(chunk) == 0:
                break
            else:
                objsize += len(chunk)

        ops = ops + 1
        t2 = time.time()
        latency = t2 - t1
        min, max, tot = latcalc(latency, min, max, tot, lat_dist)
        latencies.append(latency)

        if debug & 32:
            print "%f  %f  TransID: %s Latency: %9.6f  %s/%s" % \
                (t1, t2, transID, latency, cname, objname)

        if logmask & 1 or (logmask & 4 and latency > sizelat[size_index]):
            logger(2, "%f  %f  %s  %s/%s" % \
                       (t1, latency, transID, cname, objname), instance)

        if latency >= latexc_min and latency <= latexc_max:
            start = time.strftime('%Y%m%d %H:%M:%S', time.gmtime(t1))
            print "Host: %s -- Warning: %s GET latency exception: %6.3f " \
                "secs ObjSize: %4s TransID: %s Obj: %s/%s" % \
                (socket.gethostname(), start, latency, size, transID,
                 cname, objname)
            if options.warnexit:
                break

    elapsed = time.time() - t0
    logger(3, 'Done!  time: %f ops: %d errs: %d' % \
               (elapsed, ops, errs), instance)
    logger(9, '', instance)

    return(['get', instance, elapsed, ops, min, max, \
                tot, errs, lat_dist, latencies, scpu])


def delobj(connection, instance, donetime, cname, csize, oname, random_flag):
    """
    perform DEL operations for 1 process until end time OR requested
    number of operations is reached
    """

    lat_dist = []
    for i in range(11):
        lat_dist.append(0)

    # for stats
    latencies = []

    # we need the cpu counters for when this operation actually starts
    scpu = read_stat()

    t0 = time.time()

    min = 9999
    min = 9999
    ops = max = tot = errs = 0
    dels = 1
    maxdels = last[instance]

    logger(3, 'cname: %s  oname: %s  dels: %d  now: %d  done: %d' %
           (cname, oname, maxdels, time.time(), donetime), instance)

    while (dels <= maxdels and time.time() < donetime and errs < errmax):

        # build object name based on object type and number
        # like GET, sequential deletes of flat containers is disallowed
        if random_flag:
            object_number = randint(1, csize)
        elif re.search('f', options.objopts):
            object_number = offset + dels
        else:
            object_number = dels
        objname = '%s-%d' % (oname, object_number)

        if debug & 2:
            print "%s DEL CName: %s  OName: %s Inst: %d" %\
                (time.strftime('%H:%M:%S'), cname, objname, instance)

        dels = dels + 1
        t1 = time.time()
        try:
            response = {}
            connection.delete_object(cname, objname,
                                     response_dict=response)
            transID = response['headers']['x-trans-id']

        except ClientException, err:
            api_error('del', instance, cname, objname, err)
            errs = errs + 1
            continue

        except Exception as err:
            import traceback
            logexec('delete_object() exception: %s %s' % \
                        (err, traceback.format_exc()))
            logger(9, '', instance)
            return(['Unexpected Error - delete_object() exception: %s' % \
                        err, instance, 0, 0, 0, 0, 0, 0, \
                        lat_dist, latencies, scpu])

        ops = ops + 1
        t2 = time.time()
        latency = t2 - t1
        min, max, tot = latcalc(latency, min, max, tot, lat_dist)
        latencies.append(latency)

        if debug & 32:
            print "%f  %f  TransID: %s Latency: %9.6f  %s/%s" % \
                (t1, t2, transID, latency, cname, objname)

        if logmask & 1 or (logmask & 4 and latency > sizelat[size_index]):
            logger(2, "%f  %f  %s  %s/%s" % \
                       (t1, latency, transID, cname, objname), instance)

    elapsed = time.time() - t0
    logger(3, 'Done!  time: %f ops: %d errs: %d' % \
               (elapsed, ops, errs), instance)
    logger(9, '', instance)

    return(['del', instance, elapsed, ops, min, max, \
                tot, errs, lat_dist, latencies, scpu])


def delcont(connection, cname):
    """
    delete specified container, noting this is a cleanup function
    and NOT a test
    """

    try:
        connection.delete_container(cname)
    except ClientException, err:
        if err.http_status == 409:
            print "container %s is not empty and so couldn't delete" % cname
        else:
            print 'error %d deleting container %s' % (err.http_status, cname)
    except Exception as err:
        import traceback
        logexec('get_object() except: %s %s' % (err, traceback.format_exc()))
        logger(9, '', instance)
        print 'Unexpected Error - delete_container() exception: %s' % err


def ptime(secs):
    """
    convert time in UTC to a string of the form HH:MM:SS
    """

    string = time.ctime(secs)
    strings = string.split()
    return(strings[3])


def read_stat():
    """
    read current CPU times from /proc/stat
    """

    stats = open('/proc/stat', 'r')
    for line in stats:
        if re.match('cpu ', line):
            break

    stats.close()
    return(line.rstrip())


def build_object():
    """
    build a fixed length non-compressible object (unless --objopts
    is 'c') based on size specified by -s
    """

    # build a fixed size object of appropriate size with RANDOM bytes so we can
    # be sure they all get transfered and not compressed, but if '--objopts c'
    # use all the same so we WILL.  join() a lot faster than +
    temp = ''
    count = 0
    if not options.objopts or not re.search('c', options.objopts):
        while (count < (32 * 1024)):
            num = int(random.random() * 255)
            temp = ''.join([temp, struct.pack('B', num)])
            count = count + 1
    else:
        temp = ' ' * 32 * 1024

    # replicate it exponentially for speed
    fixed_object = temp
    while (len(fixed_object) < osize):
        fixed_object = ''.join([fixed_object, fixed_object])

    # trim it down if necessary
    fixed_object = fixed_object[:osize]

    return(fixed_object)


def execute_proc(args):
    """
    execute specified test for 1 process
    """

    instance = args[0]
    preauthurl = args[1]
    preauthtoken = args[2]
    cname = args[3]
    csize = args[4]
    oname = args[5]
    numobj = args[6]
    test = args[7]
    stime = args[8]

    # for PUTs, last will already be correct but we're also passed the correct
    # numbers anyways. but for other operations last is still pointing to the
    # reqested PUTs and not the real ones which would be different if
    # --runtime used
    last[instance] = numobj

    #    C o n n e c t

    # if connecting directly to a proxy, we need to build
    # the preauthurl in a round-robin fashion
    if len(proxies):
        proxy_index = (instance + int(options.rank)) % \
            len(proxies)
        preauthurl = 'https://%s/v1/%s' % \
            (proxies[proxy_index], project_id)

    connection = connect(endpoint, username, password, \
                             tenant_id, tenant_name, \
                             preauthurl, preauthtoken)
    if connection == -1:
        return(['Error: ClientException connect error'])
    logexec('connected')

    #     D e l a y    U n t i l    s y n c t i m e    i f    s p e c i f i e d

    # we only honor --sync for the first of a set of tests
    if first_test and options.synctime:
        wait = int(options.synctime) - time.time()
        if debug & 1:
            print "Sync: %s Wait: %f" % (options.synctime, wait)
        if wait > 0:
            # this is a lot more work than it should be.  it seems that
            # stalls of more then 10 seconds between the initial connection
            # and put/get/whatever, cause a 1 second latency.  so until fixed
            # we neen to make sure we never sleep more than 10 seconds w/o
            # some activity over the connection
            while time.time() < int(options.synctime):
                sleeptime = int(options.synctime) - time.time()
                if sleeptime > 8:
                    sleeptime = 8
                time.sleep(sleeptime)

                # should't take more than a few msec, but we're not
                # doing anything else at the moment so let's be sure
                if int(options.synctime) - time.time() > 2:
                    headers = connection.head_account()
        else:
            if not options.quiet:
                print "warning: Sync time passed..."

            # if we need to exit on this warning we need to make sure
            # output populated or bad things will happen later
            if options.warnexit:
                lat_dist = []
                for i in range(11):
                    lat_dist.append(0)
                return([test, instance, 0, 0, 0, 0, 0, 0, 0, \
                               lat_dist, read_stat()])

    if options.runtime:
        donetime = time.time() + int(options.runtime)
    else:
        donetime = 9999999999

    # turns out that some tests can take longer than the PUT, and since we
    # want to access all the objects, double our run time which should be
    # enough time for the other tests to complete
    if re.search('p', options.tests) and test != 'p':
        donetime *= 2

    #    R u n    1    T e s t

    if debug & 1:
        print "Start Test for %s - cname: %s  csize: %d  oname: %s" % \
            (test, cname, csize, oname)

    random_flag = False
    if re.match('[PGD]', test):
        random_flag = True

    if test == 'p' or test == 'P':
        logexec('begin PUT %d' % instance)
        output = put(connection, instance, donetime, cname, \
                         csize, oname, random_flag)
        logexec('PUT %d completed' % instance)
    elif test == 'g' or test == 'G':
        output = get(connection, instance, donetime, cname, \
                         csize, oname, random_flag)
    elif test == 'd' or test == 'D':
        output = delobj(connection, instance, donetime, cname, \
                         csize, oname, random_flag)
    else:
        error("Invalid test: %s" % test)

    if debug & 1:
        print "Test done for instance", instance

    connection.close()

    return(output)


def print_line(procs, instance, ops, rate, iops, min, max, tot, errs,
               cpu_percent, lat_dist, median, etime, psum_flag=False):
    """
    print a line of results for one process OR total for all, only printing
    process details when --psum set and print_output tells us to do so
    """

    # convert procs to a string so we can print a '-' for --psum
    if not psum_flag:
        pstring = '%s' % procs
    else:
        pstring = '-'

    # relatively rare, but if no ops, no latencies...
    if ops:
        latency = "%7.3f" % float(tot / ops)
    else:
        latency = '000.00'
        min = max = 0
        for i in range(11):
            lat_dist.append(0)

    if test == 'p':
        tname = 'put'
    elif test == 'P':
        tname = 'putR'
    elif test == 'g':
        tname = 'get'
    elif test == 'G':
        tname = 'getR'
    elif test == 'd':
        tname = 'del'
    elif test == 'D':
        tname = 'delR'

    line = ''
    if options.rank:
        line += "%-4s " % options.rank
    line += "%-4s  %4d %4s %6s  %8s  %8s %8.2f %5d" % \
        (tname, 1, procs, cvt2KMG(osize), ptime(stime), ptime(etime), \
             rate, ops)
    line += "%10.2f %4d %s %7.3f %5.2f-%05.2f" % \
        (iops, errs, latency, median, min, max)
    if options.ldist:
        for i in range(11):
            line += " %5d" % lat_dist[i]

    if options.nocompress:
        compress = 'no'
    else:
        compress = 'def'
    line += "  %5.2f  %4s" % (cpu_percent, compress)
    if options.utc:
        line += ' %d' % ttime
    print line


def median_calc(list):
    """
    Calculate the median of a list
    """

    list.sort()
    return(list[len(list) / 2])


def print_output(results, procs):
    """
    print header AND generate results for 1 process or summarize all,
    calling print_line() for each
    """

    global header_printed

    #    P r i n t    H e a d e r

    # only makes sense to suppress when running from a master control script
    header = ''
    if not options.nohead and not header_printed:
        if options.rank:
            header += "%4s " % 'Rank'

        header += "%4s  %4s %4s %6s  %-8s  %-8s %8s %5s" % \
            ('Test', 'Clts', 'Proc', 'OSize', 'Start', 'End', 'MB/Sec', 'Ops')
        header += "%10s %4s %7s %7s  %10s" % \
            ('Ops/Sec',  'Errs', 'Latency', 'Median', 'LatRange')

        if options.ldist:
            for i in (0, 1, 2, 3, 4, 5, 10, 20, 30, 40, 50):
                f10 = "%.*f" % (int(options.ldist), float(i) / ldist10)
                header += " %5s" % f10
        header += '   %CPU  Comp'
        if options.utc:
            header += ' %-10s' % 'Timestamp'
        print header
        header_printed = 1

    #    C a l c u l a t e    C P U    U t i l

    # first, find the oldest CPU counters based on which process started
    # first by seeing who has the lowest user time
    oldest = 9999999999
    for i in range(procs):
        scpu = results[i][10]
        user = int(scpu.split()[1])
        if user < oldest:
            cpu_start = scpu
            oldest = user

    # now get current CPU counters
    cpu_end = read_stat()
    cpus = cpu_start.split()
    cpue = cpu_end.split()

    # note that the total includes idle and iowait time
    cpu_real = cpu_total = 0
    for i in range(1, 8):
        diff = int(cpue[i]) - int(cpus[i])
        cpu_total = cpu_total + diff
        if i != 4 and i != 5:
            cpu_real = cpu_real + diff
    try:
        # I've seen failures when sync time passed and CPU elapsed = 0
        cpu_percent = 100.0 * cpu_real / cpu_total
    except ZeroDivisionError:
        cpu_percent
    #print "CPU - Real: %d  Tot: %d" % (cpu_real, cpu_total)

    #    D e a l    W i t h    E a c h    P r o c e s s

    ldist_tot = []
    for i in range(11):
        ldist_tot.append(0)

    errors = 0
    lattot = 0
    latmin = 999
    latmax = 0
    lat_all = []
    etime = time.time()
    opst = ratet = iopst = 0
    for i in range(procs):
        oper, instance, elapsed, ops, min, max, tot, errs, \
            lat_dist, latencies, scpu = results[i]

        # combine all latencies into oen big array
        lat_all += latencies

        bytes = ops * osize
        try:
            rate = bytes / elapsed / 1024 / 1024
            iops = ops / elapsed
        except ZeroDivisionError:
            rate = iops = 0

        if options.psum:
            print_line(instance, instance, ops, rate, iops, min, max,
                       tot, errs, cpu_percent, lat_dist,
                       median_calc(latencies), etime, True)

        opst += ops
        ratet += rate
        iopst += iops
        errors += errs
        lattot += tot

        # even if we don't need it
        for i in range(11):
            ldist_tot[i] += lat_dist[i]

        if min < latmin:
            latmin = min
        if max > latmax:
            latmax = max
        i = i + 1

    # Final tally, always 1 greater than last one but if no iops,
    # no median value
    if iopst:
        median = median_calc(lat_all)
    else:
        median = 0

    print_line(procs, instance + 1, opst, ratet, iopst, latmin, latmax,
               lattot, errors, cpu_percent, ldist_tot, median, etime)


def control_c_handler(signal, frame):

    # on ^C, just use a big stuck and whack the parent and that will
    # bring down all the children.
    os.kill(ppid, 9)
    sys.exit(0)

#####################################
#    S T A R T    O F    S C R I P T
#####################################

global header_printed

if __name__ == "__main__":

    version = '0.0.7'
    copyright = 'Copyright 2013 Hewlett-Packard Development Company, L.P.'

    # need to differentiate initial call to main from those that are called
    # when parsing args during multiprocessing
    main(sys.argv[1:])

    # we probably could do for a single server but it would have to be rank 0
    if re.search('f', options.objopts) and re.search('[gd]', options.tests):
        error("--objopts f not supported for sequential gets/dels")

    if re.search('a', options.objopts):
        if re.search('P', options.tests):
            error("append mode makes no sense for random PUTs")
        elif not re.search('f', options.objopts):
            error("append mode only supported for flat hierarchies" + \
                      "consider different onames OR --objopts u")

    #     C r e a t e    L o c a l    C o n n e c t i o n

    # multiprocessing/ssl doesn't like to use the same connections in the
    # parent and child processes, so create one here for us to use.
    # also use this opportunity to get a single auth token/url pair for
    # everyone to share
    proxies = []
    if options.proxies == '':
        if options.preauthtoken != '':
            error('use of --preauthtoken only makes sense with --proxies')

        connection = connect(endpoint, username, password, \
                                 tenant_id, tenant_name)
        if connection == -1:
            error('Error: ClientException connect error')
        preauthtoken = connection.token
        preauthurl = connection.url

    # if talking directly to proxies, build a list of them which
    # we'll later plug into the preauthurl
    else:
        for addr in options.proxies.split(','):
            proxies.append(addr)

        # unless explicitly directed not to use an token, generate an
        # auth-token using the supplied username/password
        preauthurl = None
        if options.preauthtoken == '':
            connection = connect(endpoint, username, password, \
                                     tenant_id, tenant_name)
            if connection == -1:
                error('Error: ClientException connect error')
            preauthtoken = connection.token
        else:
            preauthtoken = options.preauthtoken
            preauthurl = "https://%s/v1/%s" % (proxies[0], username)
            connection = connect(endpoint, username, password, \
                                     tenant_id, tenant_name, \
                                     preauthurl, preauthtoken)
            if connection == -1:
                error('Error: ClientException connect error')

        # when using any sort of auth token we no longer use username/auth
        project_id = username.split(':')[0]
        username = password = ''

    if options.repeats:
        repeats = int(options.repeats)
    else:
        repeats = 1

    try:
        fields = options.logops.split(':')
        logmask = int(fields[0])
    except:
        error('--logops must be an integer')

    if logmask & 4:
        sizelat = []
        sizes = len(sizeset)
        opslat = len(fields) - 1
        if opslat > 1 and opslat != sizes:
            error("you have specified more then one latency with opslogs " + \
                      "but their count doesn't match number of sizes")
        for i in range(sizes):
            try:
                if opslat == ':1':
                    sizelat.append(float(fields[1]))
                else:
                    sizelat.append(float(fields[i + 1]))
            except:
                error("--logops 4 must include ':val' for latency exceptions")

    logexec('Beginning execution for procset: %s' % options.procset)

    # save our pid which is parent to the subprocesses and set a ^C handler
    ppid = os.getpid()
    signal.signal(signal.SIGINT, control_c_handler)

    last_size = 0
    first_test = 1
    header_printed = 0
    for rep in range(repeats):
        for procs in procset:

            logexec('Running tests for %d procs' % procs)

            # this resets last[] for the upcoming set of tests and start a
            # a new section of output with a new header unless --repeat
            reset_last(procs)

            if repeats == 1:
                header_printed = 0

            for size_index in range(len(sizeset)):
                size = sizeset[size_index]
                osize = cvtFromKMG(size)

                # see if we need to build a new test object
                if osize != last_size:
                    fixed_object = build_object()
                    last_size = osize

                puts_per_proc = []
                for test in options.tests.split(','):

                    # what time with execution actually start?
                    if options.synctime:
                        stime = int(options.synctime)
                    else:
                        stime = time.time()

                    # and the timestamp is tricky.  For PUT test, it's the test
                    # time.  For all others if the container ends in what looks
                    # like a utc time, use that.
                    log_time = stime
                    if test != 'p' and re.search('-\d{10}$', options.cname):
                        log_time = options.cname.split('-')[-1]

                    # we want the logs to match the test time for easy ident
                    # and also need to preallocate the list of log file handles
                    if logmask:
                        logfiles = []
                        for i in range(1, procs + 1):
                            logfiles.append(0)
                            logger(1, test, i - 1, log_time)

                    jobs = []
                    pool = Pool(procs)
                    inputs = []
                    csize = last_size = 0
                    create_container = 0
                    created = {}
                    for inst in range(procs):
                        puts_per_proc.append(0)    # preallocate
                        if test == 'p' or not re.search('p', options.tests):
                            ttime = stime    # all tests get same ttime w/ UTC
                            numobj = last[inst]
                        else:
                            numobj = puts_per_proc[inst]

                        if debug & 16:
                            print "Exec - I: %d Obj: %d Test: %s Time: %f" % \
                                (inst, numobj, test, stime)

                        cname = options.cname
                        if options.utc:
                            cname += '-%d' % ttime
                        if options.ctype == 'bynode':
                            cname += "-%s" % options.rank
                        elif options.ctype == 'byproc':
                            cname += "-%s-%d" % (options.rank, inst)

                        # make sure container for get/delete tests exist
                        # before proceeding.  appending on PUT needs to be
                        # dealt with later.
                        if re.search('[gGdD]', test):
                            try:
                                container = connection.head_container(cname)
                            except ClientException, err:
                                if err.http_status == 404:
                                    error("container '%s' doesn't exist" % \
                                        cname)
                                else:
                                    error("Error %s trying to access '%s'" % \
                                         (err.http_status, cname))

                        # if not doing random or flat object I/O, oject names
                        # all start with base-rank-inst
                        oname = options.oname
                        if re.search('u', options.objopts):
                            oname += '-%d' % ttime
                        if not re.match('[PGD]', test) and \
                                not re.search('f', options.objopts):
                            oname += "-%s-%d" % (options.rank, inst)

                        if debug & 8:
                            print 'debug: cname %s  oname: %s' % (cname, oname)

                        # for flat hierarchies or when in append mode (which
                        # assumes flat hierarchies), we need to know if
                        # container exists and if so, how many objects.
                        # since all procs for shared|bynode write to container
                        # of same name we only check size once.  also note
                        # each client does this check so if something does go
                        # wrong you can get multiple errors
                        if re.search('[af]', options.objopts):
                            if inst == 0 or options.ctype == 'byproc':
                                try:
                                    count_key = 'x-container-object-count'
                                    container = \
                                        connection.head_container(cname)
                                    csize = int(container[count_key])
                                except ClientException, err:
                                    if err.http_status == 404 and \
                                            not cname in created:
                                        warn = "'%s' in append mode" % cname
                                        print "warning: creating '%s'" % warn
                                        created[cname] = ''
                                    else:
                                        etype = "head_container error: "
                                        error("%s %s on '%s" % \
                                                  (etype, err, cname))

                        # make sure container(s) exits BEFORE tests start,
                        # noting append mode sets csize IF continer exists
                        if test == 'p' and csize == 0:
                            if inst == 0 or options.ctype == 'byproc':
                                try:
                                    # in case a lot of containers, stagger
                                    # creation to be safe, but not too much
                                    time.sleep(.01)
                                    logexec('Create container %d' % inst)
                                    create_container = 1
                                    connection.put_container(cname)
                                    logexec('container %d created' % inst)
                                except Exception as err:
                                    import traceback
                                    logexec('put_container except: %s %s' % \
                                                (err, traceback.format_exc()))
                                    error('Error: put_container exc: %s' % err)

                        inputs.append([inst, preauthurl, preauthtoken, cname, \
                                           csize, oname, numobj, test, stime])

                    poolOutputs = pool.map(execute_proc, inputs)
                    pool.close()
                    pool.join()
                    first_test = 0

                    results = []
                    total_errors = 0
                    unexpected_error = 0
                    for i in range(procs):
                        logexec('I: %s' % i)
                        oneproc = poolOutputs[i]
                        if re.search('error', oneproc[0], re.IGNORECASE):
                            print "%s, try -d128 for more clues " % oneproc[0],
                            print "in /tmp on remote node"
                            unexpected_error = 1
                            break

                        results.append(oneproc)
                        logexec('I: %s got output %s' % (i, oneproc[0]))
                        total_errors += oneproc[7]
                        logexec('Count Errors: %d' % total_errors)

                        # for PUT test save number of objs actually written in
                        # case we terminated due to a timer rather than count
                        if test == 'p':
                            instance = oneproc[1]
                            nobjects = oneproc[3]
                            puts_per_proc[instance] = nobjects

                    if unexpected_error:
                        continue

                    print_output(results, procs)
                    logexec('printing complete')

                    if test == 'p' and options.putsperproc:
                        ppp = ''
                        for puts in puts_per_proc:
                            ppp += '%d:' % puts
                        print "PutsPerProc: %s" % ppp[:-1]

                    # unless --cont-nodelete, delete container(s) after
                    # delete test run
                    if test == 'd' and not options.cont_nodelete:
                        cname = options.cname
                        if options.utc:
                            cname += '-%d' % ttime
                        if options.ctype == 'bynode':
                            cname += "-%s" % options.rank
                        if debug & 1:
                            print 'deleting container(s): %s' % cname

                        if options.ctype != 'byproc':
                            delcont(connection, cname)
                        else:
                            for proc in range(procs):
                                name = '%s-%s-%d' % (cname, options.rank, proc)
                                delcont(connection, name)

                    # not sure if I should do this last, but I think I'd like
                    # to let all processes finish as well as all prints to
                    # complete before aborting, which is what warnproc means
                    if total_errors > 0 and options.warnexit:
                        sys.exit()

    logexec('processing complete')