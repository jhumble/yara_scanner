#!/usr/bin/env python3
# vim: sw=4:ts=4:et:cc=120
__version__ = '1.0.15'
__doc__ = """
Yara Scanner
============

A wrapper around the yara library for Python. ::

    scanner = YaraScanner()
    # start tracking this yara file
    scanner.track_yara_file('/path/to/yara_file.yar')
    scanner.load_rules()
    scanner.scan('/path/to/file/to/scan')

    # check to see if your yara file changed
    if scanner.check_rules():
        scanner.load_rules()

    # track an entire directory of yara files
    scanner.track_yara_dir('/path/to/directory')
    scanner.load_rules()
    # did any of the yara files in this directory change?
    if scanner.check_rules():
        scanner.load_rules()

    # track a git repository of yara rules
    scanner.track_yara_repo('/path/to/git_repo')
    scanner.load_rules()
    # this only returns True if a new commit was added since the last check
    if scanner.check_rules():
        scanner.load_rules()

"""

import datetime
import functools
import json
import logging
import multiprocessing
import os
import os.path
import pickle
import random
import re
import shutil
import signal
import socket
import struct
import threading
import time
import traceback
from subprocess import PIPE, Popen

import plyara
import yara

# keys to the JSON dicts you get back from YaraScanner.scan_results
RESULT_KEY_TARGET = 'target'
RESULT_KEY_META = 'meta'
RESULT_KEY_NAMESPACE = 'namespace'
RESULT_KEY_RULE = 'rule'
RESULT_KEY_STRINGS = 'strings'
RESULT_KEY_TAGS = 'tags'
ALL_RESULT_KEYS = [
    RESULT_KEY_TARGET,
    RESULT_KEY_META,
    RESULT_KEY_NAMESPACE,
    RESULT_KEY_RULE,
    RESULT_KEY_STRINGS,
    RESULT_KEY_TAGS,
]

yara.set_config(max_strings_per_rule=30720)
log = logging.getLogger('yara-scanner')

def get_current_repo_commit(repo_dir):
    """Utility function to return the current commit hash for a given repo directory.  Returns None on failure."""
    p = Popen(['git', '-C', repo_dir, 'log', '-n', '1', '--format=oneline'], stdout=PIPE, stderr=PIPE, universal_newlines=True)
    commit, stderr= p.communicate()
    p.wait()

    if len(stderr.strip()) > 0:
        log.error("git reported an error: {}".format(stderr.strip()))

    if len(commit) < 40:
        log.error("got {} for stdout with git log".format(commit.strip()))
        return None

    return commit[0:40]

class YaraJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, bytes):
            return o.decode('utf-8', errors='backslashreplace')

        return json.JSONEncoder.default(self, o)

class YaraScanner(object):
    """
    The primary object used for scanning files and data with yara rules."""
    def __init__(self, signature_dir=None, thread_count=None, test_mode=False):
        """
        Creates a new YaraScanner object.

        :param signature_dir: A directory that contains one directory per set of yara rules. Each subdirectory will get loaded into its own namespace (named after the path to the directory.) This is for convenience. Also see :func:`YaraScanner.track_yara_file`, :func:`YaraScanner.track_yara_dir`, and :func:`YaraScanner.track_yara_repo`.

        :type signature_dir: str or None
        
        :param thread_count: Number of threads to use. *This parameter is ignored and no longer used.*
        
        :param test_mode: When set to True, each yara rule is tested for performance issued when it is loaded.
        
        :type test_mode: bool

        """
        self.rules = None
        self._scan_results = []

        # we can pass in a list of "blacklisted" rules
        # this is a list of rule NAMES that are essentially ignored in the scan results (not output)
        self._blacklisted_rules = set()

        # we keep track of when the rules change and (optionally) automatically re-load the rules
        self.tracked_files = {} # key = file_path, value = last modification time
        self.tracked_dirs = {} # key = dir_path, value = {} (key = file_path, value = last mtime)
        self.tracked_repos = {} # key = dir_path, value = current git commit

        # both parameters to this function are for backwards compatibility
        if thread_count is not None:
            log.warning("thread_count is no longer used in YaraScanner.__init__")

        # if we are in test mode, we test each yara file as it is loaded for performance issues
        self.test_mode = test_mode
        
        if signature_dir is not None:
            for dir_path in os.listdir(signature_dir):
                dir_path = os.path.join(signature_dir, dir_path)
                if not os.path.isdir(dir_path):
                    continue

                if os.path.exists(os.path.join(dir_path, '.git')):
                    self.track_yara_repository(dir_path)
                else:
                    self.track_yara_dir(dir_path)

    @property
    def scan_results(self):
        """Returns the scan results of the most recent scan.

        This function returns a list of dict with the following format ::

            {
                'target': str,
                'meta': dict,
                'namespace': str,
                'rule': str,
                'strings': list,
                'tags': list,
            }
        
        **target** is the target of the scane. In the case of file scans then target will be the path to the file that was scanned. In the case of data (raw binary) scans, this will be an empty string.

        **meta** is the dict of meta directives of the matching rule.

        **namespace** is the namespace the rule is in. In the case of repo and directory tracking, this will be the path of the directory. Otherwise it has a hard coded value of DEFAULT. *Setting the namespace to the path of the directory allows yara rules with duplicate names in different directories to be added to the same yara context.*

        **rule** is the name of the matching yara rule.

        **strings** is a list of tuples representing the individual string matches in the following format. ::

            (position, string_name, content)

        where **position** is the byte position of the match, **string_name** is the name of the yara string that matched, and **content** is the binary content it matched.

        **tags** is a list of tags contained in the matching rule.
        """
        return self._scan_results

    @scan_results.setter
    def scan_results(self, value):
        self._scan_results = value

    @property
    def blacklist(self):
        """The list of yara rules configured as blacklisted. Rules that are blacklisted are not compiled and used."""
        return list(self._blacklisted_rules)

    @blacklist.setter
    def blacklist(self, value):
        assert isinstance(value, list)
        self._blacklisted_rules = set(value)

    def blacklist_rule(self, rule_name):
        """Adds the given rule to the list of rules that are blacklisted. See :func:`YaraScanner.blacklist`."""
        self._blacklisted_rules.add(rule_name)

    @property
    def json(self):
        """Returns the current scan results as a JSON formatted string."""
        return json.dumps(self.scan_results, indent=4, sort_keys=True, cls=YaraJSONEncoder)

    @functools.lru_cache()
    def git_available(self):
        """Returns True if git is available on the system, False otherwise."""
        return shutil.which('git')

    def track_yara_file(self, file_path):
        """Adds a single yara file.  The file is then monitored for changes to mtime, removal or adding."""
        if not os.path.exists(file_path):
            self.tracked_files[file_path] = None # file did not exist when we started tracking
            # we keep track of the path by keeping the key in the dictionary
            # so that if the file comes back we'll reload it
        else:
            self.tracked_files[file_path] = os.path.getmtime(file_path)

        log.debug("yara file {} tracked @ {}".format(file_path, self.tracked_files[file_path]))

    def track_yara_dir(self, dir_path):
        """Adds all files in a given directory that end with .yar when converted to lowercase.  All files are monitored for changes to mtime, as well as new and removed files."""
        if not os.path.isdir(dir_path):
            log.error("{} is not a directory".format(dir_path))
            return

        self.tracked_dirs[dir_path] = {}

        for file_path in os.listdir(dir_path):
            file_path = os.path.join(dir_path, file_path)
            if file_path.lower().endswith('.yar') or file_path.lower().endswith('.yara'):
                self.tracked_dirs[dir_path][file_path] = os.path.getmtime(file_path)
                log.debug("tracking file {} @ {}".format(file_path, self.tracked_dirs[dir_path][file_path]))

        log.debug("tracking directory {} with {} yara files".format(dir_path, len(self.tracked_dirs[dir_path])))

    def track_yara_repository(self, dir_path):
        """Adds all files in a given directory **that is a git repository** that end with .yar when converted to lowercase.  Only commits to the repository trigger rule reload."""
        if not self.git_available():
            log.warning("git cannot be found: defaulting to track_yara_dir")
            return self.track_yara_dir(dir_path)

        if not os.path.isdir(dir_path):
            log.error("{} is not a directory".format(dir_path))
            return False

        if not os.path.exists(os.path.join(dir_path, '.git')):
            log.error("{} is not a git repository (missing .git)".format(dir_path))
            return False

        # get the initial commit of this directory
        self.tracked_repos[dir_path] = get_current_repo_commit(dir_path)
        log.debug("tracking git repo {} @ {}".format(dir_path, self.tracked_repos[dir_path]))

    def check_rules(self):
        """
        Returns True if the rules need to be recompiled, False otherwise. The criteria that determines if the rules are recompiled depends on how they are tracked.
        
        :rtype: bool"""

        reload_rules = False # final result to return

        for file_path in self.tracked_files.keys():
            # did the file come back?
            if self.tracked_files[file_path] is None and os.path.exists(file_path):
                log.info(f"detected recreated yara file {file_path}")
                self.track_yara_file(file_path)
            # was the file deleted?
            elif self.tracked_files[file_path] is not None and not os.path.exists(file_path):
                log.info(f"detected deleted yara file {file_path}")
                self.track_yara_file(file_path)
                reload_rules = True
            # was the file modified?
            elif os.path.getmtime(file_path) != self.tracked_files[file_path]:
                log.info(f"detected change in yara file {file_path}")
                self.track_yara_file(file_path)
                reload_rules = True

        for dir_path in self.tracked_dirs.keys():
            reload_dir = False # set to True if we need to reload this directory
            existing_files = set() # keep track of the ones we see
            for file_path in os.listdir(dir_path):
                file_path = os.path.join(dir_path, file_path)
                if not ( file_path.lower().endswith('.yar') or file_path.lower().endswith('.yara') ):
                    continue

                existing_files.add(file_path)
                if file_path not in self.tracked_dirs[dir_path]:
                    log.info("detected new yara file {} in {}".format(file_path, dir_path))
                    reload_dir = True
                    reload_rules = True

                elif os.path.getmtime(file_path) != self.tracked_dirs[dir_path][file_path]:
                    log.info("detected change in yara file {} dir {}".format(file_path, dir_path))
                    reload_dir = True
                    reload_rules = True

            # did a file get deleted?
            for file_path in self.tracked_dirs[dir_path].keys():
                if file_path not in existing_files:
                    log.info("detected deleted yara file {} in {}".format(file_path, dir_path))
                    reload_dir = True
                    reload_rules = True

            if reload_dir:
                self.track_yara_dir(dir_path)

        for repo_path in self.tracked_repos.keys():
            current_repo_commit = get_current_repo_commit(repo_path)
            #log.debug("repo {} current commit {} tracked commit {}".format(
                #repo_path, self.tracked_repos[repo_path], current_repo_commit))

            if current_repo_commit != self.tracked_repos[repo_path]:
                log.info("detected change in git repo {}".format(repo_path))
                self.track_yara_repository(repo_path)
                reload_rules = True

        # if we don't have a yara context yet then we def need to compile the rules
        if self.rules is None:
            return True
            
        return reload_rules

    def load_rules(self):
        """
        Loads and compiles all tracked yara rules. Returns True if the rules were loaded correctly, False otherwise.

        Scans can be performed only after the rules are loaded.

        :rtype: bool"""

        # load and compile the rules
        # we load all the rules into memory as a string to be compiled
        sources = {}
        rule_count = 0
        file_count = 0

        # get the list of all the files to compile
        all_files = {} # key = "namespace", value = [] of file_paths
        # XXX there's a bug in yara where using an empty string as the namespace causes a segfault
        all_files['DEFAULT'] = [ _ for _ in self.tracked_files.keys() if self.tracked_files[_] is not None]
        file_count += len(all_files['DEFAULT'])
        for dir_path in self.tracked_dirs.keys():
            all_files[dir_path] = self.tracked_dirs[dir_path]
            file_count += len(self.tracked_dirs[dir_path])

        for repo_path in self.tracked_repos.keys():
            all_files[repo_path] = []
            for file_path in os.listdir(repo_path):
                file_path = os.path.join(repo_path, file_path)
                if file_path.lower().endswith('.yar') or file_path.lower().endswith('.yara'):
                    all_files[repo_path].append(file_path)
                    file_count += 1

        # if we have no files to compile then we have nothing to do
        if file_count == 0:
            logging.debug("no files to compile")
            self.rules = None
            return False

        if self.test_mode:
            execution_times = [] # of (total_seconds, buffer_type, file_name, rule_name)
            execution_errors = [] # of (error_message, buffer_type, file_name, rule_name)
            random_buffer = os.urandom(1024 * 1024) # random data to scan

        for namespace in all_files.keys():
            for file_path in all_files[namespace]:
                with open(file_path, 'r') as fp:
                    log.debug("loading namespace {} rule file {}".format(namespace, file_path))
                    data = fp.read()

                    try:
                        # compile the file as a whole first, make sure that works
                        rule_context = yara.compile(source=data)
                        rule_count += 1
                    except Exception as e:
                        log.error("unable to compile {}: {}".format(file_path, str(e)))
                        continue

                    if self.test_mode:
                        parser = plyara.Plyara()
                        parsed_rules = { } # key = rule_name, value = parsed_yara_rule
                        for parsed_rule in parser.parse_string(data):
                            parsed_rules[parsed_rule['rule_name']] = parsed_rule

                        for rule_name in parsed_rules.keys():
                            # some rules depend on other rules, so we deal with that here
                            dependencies = [] # list of rule_names that this rule needs
                            rule_context = None

                            while True:
                                # compile all the rules we've collected so far as one
                                dep_source = '\n'.join([parser.rebuild_yara_rule(parsed_rules[r]) 
                                                        for r in dependencies])
                                try:
                                    rule_context = yara.compile(source='{}\n{}'.format(dep_source, 
                                                   parser.rebuild_yara_rule(parsed_rules[rule_name])))
                                    break
                                except Exception as e:
                                    # some rules depend on other rules
                                    m = re.search(r'undefined identifier "([^"]+)"', str(e))
                                    if m:
                                        dependency = m.group(1)
                                        if dependency in parsed_rules:
                                            # add this rule to the compilation and try again
                                            dependencies.insert(0, dependency)
                                            continue
                                
                                    log.warning("rule {} in file {} does not compile by itself: {}".format(
                                                rule_name, file_path, e))
                                    rule_context = None
                                    break

                            if not rule_context:
                                continue

                            if dependencies:
                                log.info("testing {}:{},{}".format(file_path, rule_name, ','.join(dependencies)))
                            else:
                                log.info("testing {}:{}".format(file_path, rule_name))
                            
                            start_time = time.time()
                            try:
                                rule_context.match(data=random_buffer, timeout=5)
                                end_time = time.time()
                                total_seconds = end_time - start_time
                                execution_times.append((total_seconds, 'random', file_path, rule_name))
                            except Exception as e:
                                execution_errors.append((str(e), 'random', file_name))

                            for x in range(255):
                                byte_buffer = bytes([x]) * (1024 * 1024)
                                start_time = time.time()
                                try:
                                    rule_context.match(data=byte_buffer, timeout=5)
                                    end_time = time.time()
                                    total_seconds = end_time - start_time
                                    execution_times.append((total_seconds, 'byte({})'.format(x), file_path, rule_name))
                                except Exception as e:
                                    execution_errors.append((str(e), 'byte({})'.format(x), file_path, rule_name))
                                    # if we fail once we break out
                                    break
                        
                    # then we just store the source to be loaded all at once in the compilation that gets used
                    if namespace not in sources:
                        sources[namespace] = []

                    sources[namespace].append(data)

        if self.test_mode:
            execution_times = sorted(execution_times, key=lambda x: x[0])
            for execution_time, buffer_type, file_path, yara_rule in execution_times:
                print("{}:{} <{}> {}".format(file_path, yara_rule, buffer_type, execution_time))

            for error_message, buffer_type, file_path, yara_rule in execution_errors:
                print("{}:{} <{}> {}".format(file_path, yara_rule, buffer_type, error_message))

            return False

        for namespace in sources.keys():
            sources[namespace] = '\r\n'.join(sources[namespace])

        try:
            log.info("loading {} rules".format(rule_count))
            self.rules = yara.compile(sources=sources)
            return True
        except Exception as e:
            log.error("unable to compile all yara rules combined: {}".format(str(e)))
            self.rules = None
            return False

    # we're keeping things backwards compatible here...
    def scan(self, 
        file_path, 
        yara_stdout_file=None,
        yara_stderr_file=None,
        external_vars={}):
        """
        Scans the given file with the loaded yara rules. Returns True if at least one yara rule matches, False otherwise.

        The ``scan_results`` property will contain the results of the scan.

        :param file_path: The path to the file to scan.
        :type file_path: str
        :param yara_stdout_file: Ignored.
        :param yara_stderr_file: Ignored.
        :external_vars: dict of variables to pass to the scanner as external yara variables (typically used in the condition of the rule.)
        :type external_vars: dict
        :rtype: bool
        """

        assert self.rules is not None

        # scan the file
        # external variables come from the profile points added to the file
        yara_matches = self.rules.match(file_path, externals=external_vars, timeout=5)
        return self._scan(file_path, None, yara_matches, yara_stdout_file, yara_stderr_file, external_vars)

    def scan_data(self,
        data,
        yara_stdout_file=None,
        yara_stderr_file=None,
        external_vars={}):
        """
        Scans the given data with the loaded yara rules. ``data`` can be either a str or bytes object. Returns True if at least one yara rule matches, False otherwise.

        The ``scan_results`` property will contain the results of the scan.

        :param data: The data to scan.
        :type data: str or bytes
        :param yara_stdout_file: Ignored.
        :param yara_stderr_file: Ignored.
        :external_vars: dict of variables to pass to the scanner as external yara variables (typically used in the condition of the rule.)
        :type external_vars: dict
        :rtype: bool
        """

        assert self.rules is not None

        # scan the data stream
        # external variables come from the profile points added to the file
        yara_matches = self.rules.match(data=data, externals=external_vars, timeout=5)
        return self._scan(None, data, yara_matches, yara_stdout_file, yara_stderr_file, external_vars)

    def _scan(self, 
        file_path, 
        data,
        yara_matches,
        yara_stdout_file=None,
        yara_stderr_file=None,
        external_vars={}):

        # if we didn't specify a file_path then we default to an empty string
        # that will be the case when we are scanning a data chunk
        if file_path is None:
            file_path = ''

        # the mime type of the file
        # we'll figure it out if we need to
        mime_type = None

        # the list of matches after we filter
        self.scan_results = []

        for match_result in yara_matches:
            skip = False # state flag

            # is this a rule we've blacklisted?
            if match_result.rule in self.blacklist:
                log.debug("rule {} is blacklisted".format(match_result.rule))
                continue

            for directive in match_result.meta:
                value = match_result.meta[directive]

                # everything we're looking for is a string
                if not isinstance(value, str):
                    continue

                # you can invert the logic by starting the value with !
                inverted = False
                if value.startswith('!'):
                    value = value[1:]
                    inverted = True

                # you can use regex by starting string with re: (after optional negation)
                use_regex = False
                if value.startswith('re:'):
                    value = value[3:]
                    use_regex = True

                # or you can use substring matching with sub:
                use_substring = False
                if value.startswith('sub:'):
                    value = value[4:]
                    use_substring = True

                # figure out what we're going to compare against
                compare_target = None
                if directive.lower() == 'file_ext':
                    if '.' not in file_path:
                        compare_target = ''
                    else:
                        compare_target = file_path.rsplit('.', maxsplit=1)[1]

                elif directive.lower() == 'mime_type':
                    # have we determined the mime type for this file yet?
                    if mime_type is None:
                        if not file_path:
                            mime_type = ''
                        else:
                            p = Popen(['file', '-b', '--mime-type', file_path], stdout=PIPE)
                            mime_type = p.stdout.read().decode().strip()
                            log.debug("got mime type {} for {}".format(mime_type, file_path))

                    compare_target = mime_type

                elif directive.lower() == 'file_name':
                    compare_target = os.path.basename(file_path)

                elif directive.lower() == 'full_path':
                    compare_target = file_path

                else:
                    # not a meta tag we're using
                    #log.debug("not a valid meta directive {}".format(directive))
                    continue

                log.debug("compare target is {} for directive {}".format(compare_target, directive))

                # figure out how to compare what is supplied by the user to the search target
                if use_regex:
                    compare_function = lambda user_supplied, target: re.search(user_supplied, target)
                elif use_substring:
                    compare_function = lambda user_supplied, target: user_supplied in target
                else:
                    compare_function = lambda user_supplied, target: user_supplied.lower() == target.lower()

                matches = False
                for search_item in [x.strip() for x in value.lower().split(',')]:
                    matches = matches or compare_function(search_item, compare_target)
                    #log.debug("search item {} vs compare target {} matches {}".format(search_item, compare_target, matches))

                if ( inverted and matches ) or ( not inverted and not matches ):
                    log.debug("skipping yara rule {} for file {} directive {} list {} negated {} regex {} subsearch {}".format(
                        match_result.rule, file_path, directive, value, inverted, use_regex, use_substring))
                    skip = True
                    break # we are skipping so we don't need to check anything else

            if not skip:
                self.scan_results.append(match_result)

        # get rid of the yara object and just return dict
        # also includes a "target" (reference to what was scanned)
        self.scan_results = [{
            'target': file_path,
            'meta': o.meta,
            'namespace': o.namespace,
            'rule': o.rule,
            'strings': o.strings,
            'tags': o.tags } for o in self.scan_results]

        # this is for backwards compatible support
        if yara_stdout_file is not None:
            try:
                with open(yara_stdout_file, 'w') as fp:
                    json.dump(self.scan_results, fp, indent=4, sort_keys=True)
            except Exception as e:
                log.error("unable to write to {}: {}".format(yara_stdout_file, str(e)))
            
        return self.has_matches

    @property
    def has_matches(self):
        return len(self.scan_results) != 0

# typically you might want to start a process, load the rules, then fork() for each client to scan
# the idea being the each child process will be reusing the same yara rules loaded in memory
# in practice, the yara rules compile into some kind of huge blob inside libyara
# and the amount of time it takes the kernel to the clone() seems to gradually increase as a result of that
# so the rules are loaded into each process and are reused until re-loaded

#
# each scanner listens on a local unix socket for new things to scan
# once connected the following protocol is observed
# client sends one byte with the following possible values
# 1) what follows is a data stream
# 2) what follows is a file path
# in either case the client sends an unsigned integer in network byte order
# that is the size of the following data (either data stream or file name)
# finally the client sends another unsigned integer in network byte order
# followed by a JSON hash of all the external variables to define for the scan
# a size of 0 would indicate an empty JSON file
# 
# once received the scanner will scan the data (or the file) and submit a result back to the client
# the result will be a data block with one of the following values
# * an empty block meaning no matches
# * a pickled exception for yara scanning failures
# * a pickled result dictionary
# then the server will close the connection


COMMAND_FILE_PATH = b'1'
COMMAND_DATA_STREAM = b'2'

DEFAULT_BASE_DIR = '/opt/yara_scanner'
DEFAULT_SIGNATURE_DIR = '/opt/signatures'
DEFAULT_SOCKET_DIR = 'socket'

class YaraScannerServer(object):
    def __init__(self, base_dir=DEFAULT_BASE_DIR, signature_dir=DEFAULT_SIGNATURE_DIR, socket_dir=DEFAULT_SOCKET_DIR, 
                 update_frequency=60, backlog=50):

        # set to True to gracefully shutdown
        self.shutdown = multiprocessing.Event()

        # set to True to gracefully shutdown the current scanner (used for reloading)
        self.current_scanner_shutdown = None # threading.Event

        # primary scanner controller
        self.process_manager = None

        # list of YaraScannerServer Process objects
        # there will be one per cpu available as returned by multiprocessing.cpu_count()
        self.servers = [None for _ in range(multiprocessing.cpu_count())]

        # base directory of yara scanner
        self.base_dir = base_dir

        # the directory that contains the signatures to load
        self.signature_dir = signature_dir

        # the directory that contains the unix sockets
        self.socket_dir = socket_dir

        # how often do we check to see if the yara rules changed? (in seconds)
        self.update_frequency = update_frequency
        
        # parameter to the socket.listen() function (how many connections to backlog)
        self.backlog = backlog

        #
        # the following variables are specific to the child proceses
        #

        # the "cpu index" of this process (used to determine the name of the unix socket)
        self.cpu_index = None

        # the path to the unix socket this process is using
        self.socket_path = None

        # the socket we are listening on for scan requests
        self.server_socket = None

        # the scanner we're using for this process
        self.scanner = None

        # set to True when we receive a SIGUSR1
        self.sigusr1 = False

    #
    # scanning processes die when they need to reload rules
    # this is due to what seems like a minor memory leak in the yara python library
    # so this process just watches for dead scanners and restarts them if the system isn't stopping
    #

    def run_process_manager(self):
        def _handler(signum, frame):
            self.shutdown.set()

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

        try:
            while not self.shutdown.is_set():
                try:
                    self.execute_process_manager()
                    time.sleep(0.1)
                except Exception as e:
                    log.error("uncaught exception: {}".format(e))
                    time.sleep(1)
        except KeyboardInterrupt:
            pass

        # wait for all the scanners to die...
        for server in self.servers:
            if server:
                log.info("waiting for scanner {} to exit...".format(server.pid))
                server.join()

        log.info("exiting")

    def execute_process_manager(self):
        for i, p in enumerate(self.servers):
            if self.servers[i] is not None:
                if not self.servers[i].is_alive():
                    log.info("detected dead scanner {}".format(self.servers[i].pid))
                    self.servers[i].join()
                    self.servers[i] = None

        for i, scanner in enumerate(self.servers):
            if scanner is None:
                logging.info("starting scanner on cpu {}".format(i))
                self.servers[i] = multiprocessing.Process(target=self.run, name="Yara Scanner Server ({})".format(i), args=(i,))
                self.servers[i].start()
                log.info("started scanner on cpu {} with pid {}".format(i, self.servers[i].pid))

    def initialize_server_socket(self):
        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.settimeout(1)

        # the path of the unix socket will be socket_dir/cpu_index where cpu_index >= 0
        self.socket_path = os.path.join(self.base_dir, self.socket_dir, str(self.cpu_index))
        log.info("initializing server socket on {}".format(self.socket_path))

        if os.path.exists(self.socket_path):
            try:
                os.remove(self.socket_path)
            except Exception as e:
                log.error("unable to remove {}: {}".format(self.socket_path, e))

        self.server_socket.bind(self.socket_path)
        self.server_socket.listen(self.backlog)

    def kill_server_socket(self):
        if self.server_socket is None:
            return

        try:
            log.info("closing server socket")
            self.server_socket.close()
        except Exception as e:
            log.error("unable to close server socket: {}".format(e))
        
        self.server_socket = None

        if os.path.exists(self.socket_path):
            try:
                os.remove(self.socket_path)
            except Exception as e:
                logging.error("unable to remove {}: {}".format(self.socket_path, e))
    
    def initialize_scanner(self):
        log.info("initializing scanner")
        new_scanner = YaraScanner(signature_dir=self.signature_dir)
        new_scanner.load_rules()
        self.scanner = new_scanner

    def start(self):
        self.process_manager = multiprocessing.Process(target=self.run_process_manager)
        self.process_manager.start()
        log.info("started process manager on pid {}".format(self.process_manager.pid))

    def stop(self):
        if not self.shutdown.is_set():
            self.shutdown.set()

        self.wait()
        # process manager waits for the child processes to exit so we're done at this point

    def wait(self, timeout=None):
        log.debug("waiting for process manager to exit...")
        if self.process_manager:
            self.process_manager.join()
            self.process_manager = None

    def run(self, cpu_index):
        self.cpu_index = cpu_index # starting at 0

        def _handler(signum, frame):
            self.current_scanner_shutdown.set()

        signal.signal(signal.SIGHUP, _handler)
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

        self.current_scanner_shutdown = threading.Event()

        try:
            # load up the yara scanner
            self.initialize_scanner()

            # watch for the rules to change in another thread
            self.start_rules_monitor()

            while not self.shutdown.is_set():
                try:
                    self.execute()

                    if self.current_scanner_shutdown.is_set():
                        log.info("got signal to reload rules: exiting...")
                        break

                except InterruptedError:
                    pass

                except Exception as e:
                    log.error("uncaught exception: {} ({})".format(e, type(e)))

        except KeyboardInterrupt:
            log.info("caught keyboard interrupt - exiting")

        self.stop_rules_monitor()
        self.kill_server_socket()

    def execute(self):
        # are we listening on the socket yet?
        if not self.server_socket:
            try:
                self.initialize_server_socket()
            except Exception as e:
                self.kill_server_socket()
                # don't spin the cpu on failing to allocate the socket
                self.shutdown.wait(timeout=1)
                return 

        # get the next client connection
        try:
            log.debug("waiting for client")
            client_socket, _ = self.server_socket.accept()
        except socket.timeout as e:
            # nothing came in while we were waiting (check for shutdown and try again)
            return

        try:
            self.process_client(client_socket)
        except Exception as e:
            log.error("unable to process client request: {}".format(e))
            traceback.print_exc()
        finally:
            try:
                client_socket.close()
            except Exception as e:
                log.error("unable to close client connection: {}".format(e))

    def process_client(self, client_socket):
        # read the command byte
        command = client_socket.recv(1)

        data_or_file = read_data_block(client_socket).decode()
        ext_vars = read_data_block(client_socket)

        if not ext_vars:
            ext_vars = {}
        else:
            # parse the ext vars json
            ext_vars = json.loads(ext_vars.decode())

        try:
            matches = False
            if command == COMMAND_FILE_PATH:
                log.info("scanning file {}".format(data_or_file))
                matches = self.scanner.scan(data_or_file, external_vars=ext_vars)
            elif command == COMMAND_DATA_STREAM:
                log.info("scanning {} byte data stream".format(len(data_or_file)))
                matches = self.scanner.scan_data(data_or_file, external_vars=ext_vars)
            else:
                log.error("invalid command {}".format(command))
                return
        except Exception as e:
            log.info("scanning failed: {}".format(e))
            send_data_block(client_socket, pickle.dumps(e))
            return

        if not matches:
            # a data lenghth of 0 means we didn't match anything
            send_data_block(client_socket, b'')
        else:
            # encode and submit the JSON result of the client
            #print(self.scanner.scan_results)
            send_data_block(client_socket, pickle.dumps(self.scanner.scan_results))

    def start_rules_monitor(self):
        """Starts a thread the monitor the yara rules. When it detects the yara rules
           have changed it creates a new YaraScanner and swaps it in (for self.scanner)."""

        self.rule_monitor_thread = threading.Thread(target=self.rule_monitor_loop, 
                                                    name='Scanner {} Rules Monitor'.format(self.cpu_index),
                                                    daemon=False)
        self.rule_monitor_thread.start()

    def rule_monitor_loop(self):
        log.debug("starting rules monitoring")
        counter = 0

        while True:
            if self.shutdown.is_set():
                break

            if self.current_scanner_shutdown.is_set():
                break

            if counter >= self.update_frequency:
                log.debug('checking for new rules...')
            
                # do we need to reload the yara rules?
                if self.scanner.check_rules():
                    self.current_scanner_shutdown.set()
                    break

                counter = 0

            counter += 1
            self.shutdown.wait(1)

        log.debug("stopped rules monitoring")

    def stop_rules_monitor(self):
        self.current_scanner_shutdown.set()
        self.rule_monitor_thread.join(5)
        if self.rule_monitor_thread.is_alive():
            log.error("unable to stop rule monitor thread")

def _scan(command, data_or_file, ext_vars={}, base_dir=DEFAULT_BASE_DIR, socket_dir=DEFAULT_SOCKET_DIR):
    # pick a random scanner
    # it doesn't matter which one, as long as the load is evenly distributed
    starting_index = scanner_index = random.randrange(multiprocessing.cpu_count())

    while True:
        socket_path = os.path.join(base_dir, socket_dir, str(scanner_index))

        ext_vars_json = b''
        if ext_vars:
            ext_vars_json = json.dumps(ext_vars).encode()

        client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        try:
            client_socket.connect(socket_path)
            client_socket.sendall(command)
            send_data_block(client_socket, data_or_file.encode())
            send_data_block(client_socket, ext_vars_json)

            result = read_data_block(client_socket)
            if result == b'':
                return {}

            result = pickle.loads(result)

            if isinstance(result, BaseException):
                raise result

            return result

        except socket.error as e:
            log.debug("possible restarting scanner: {}".format(e))
            # in the case where a scanner is restarting (when loading rules)
            # we will receive a socket error when we try to connect
            # just move on to the next socket and try again
            scanner_index += 1
            if scanner_index >= multiprocessing.cpu_count():
                scanner_index = 0

            # if we've swung back around wait for a few seconds and try again
            if scanner_index == starting_index:
                log.error("no scanners available")
                raise

            continue

def scan_file(path, base_dir=None, socket_dir=DEFAULT_SOCKET_DIR, ext_vars={}):
    return _scan(COMMAND_FILE_PATH, path, ext_vars=ext_vars, base_dir=base_dir, socket_dir=socket_dir)
        
def scan_data(data): # XXX ????
    return _scan(COMMAND_DATA_STREAM, data, ext_vars=ext_vars, base_dir=base_dir, socket_dir=socket_dir)

#
# protocol routines
#

def read_n_bytes(s, n):
    """Reads n bytes from socket s.  Returns the bytearray of the data read."""
    bytes_read = 0
    _buffer = []
    while bytes_read < n:
        data = s.recv(n - bytes_read)
        if data == b'':
            break

        bytes_read += len(data)
        _buffer.append(data)

    result = b''.join(_buffer)
    if len(result) != n:
        log.warning("expected {} bytes but read {}".format(n, len(result)))

    return b''.join(_buffer)

def read_data_block_size(s):
    """Reads the size of the next data block from the given socket."""
    size = struct.unpack('!I', read_n_bytes(s, 4))
    size = size[0]
    log.debug("read command block size {}".format(size))
    return size

def read_data_block(s):
    """Reads the next data block from socket s. Returns the bytearray of the data portion of the block."""
    # read the size of the data block (4 byte network order integer)
    size = struct.unpack('!I', read_n_bytes(s, 4))
    size = size[0]
    #log.debug("read command block size {}".format(size))
    # read the data portion of the data block
    return read_n_bytes(s, size)

def iterate_data_blocks(s):
    """Reads the next data block until a block0 is read."""
    while True:
        block = read_data_block(s)
        if len(block) == 0:
            raise StopIteration()
    
        yield block

def send_data_block(s, data):
    """Writes the given data to the given socket as a data block."""
    message = b''.join([struct.pack("!I", len(data)), data])
    #log.debug("sending data block length {} ({})".format(len(message), message[:64]))
    s.sendall(message)

def send_block0(s):
    """Writes an empty data block to the given socket."""
    send_data_block(s, b'')

def main():
    import argparse
    import pprint
    import sys

    #from yara_scanner import YaraScanner, YaraJSONEncoder

    parser = argparse.ArgumentParser(description="Scan the given file with yara using all available rulesets.")
    parser.add_argument('paths', metavar='PATHS', nargs="*",
        help="One or more files or directories to scan with yara.")
    parser.add_argument('-r', '--recursive', required=False, default=False, action='store_true', dest='recursive',
        help="Recursively scan directories.")
    parser.add_argument('--from-stdin', required=False, default=False, action='store_true', dest='from_stdin',
        help="Read the list of files to scan from stdin.")

    parser.add_argument('--debug', dest='log_debug', default=False, action='store_true',
        help="Log debug level messages.")
    parser.add_argument('-j', '--dump-json', required=False, default=False, action='store_true', dest='dump_json',
        help="Dump JSON details of matches.  Otherwise just list the rules that hit.")

    parser.add_argument('-t', '--test', required=False, default=False, action='store_true', dest='test',
        help="Test each yara file separately against different types of buffers for performance issues.")

    parser.add_argument('-y', '--yara-rules', required=False, default=[], action='append', dest='yara_rules',
        help="One yara rule to load.  You can specify more than one of these.")
    parser.add_argument('-Y', '--yara-dirs', required=False, default=[], action='append', dest='yara_dirs',
        help="One directory containing yara rules to load.  You can specify more than one of these.")
    parser.add_argument('-G', '--yara-repos', required=False, default=[], action='append', dest='yara_repos',
        help="One directory that is a git repository that contains yara rules to load. You can specify more than one of these.")
    parser.add_argument('-c', '--compile-only', required=False, default=False, action='store_true', dest='compile_only',
        help="Compile the rules and exit.")
    parser.add_argument('-b', '--blacklist', required=False, default=[], action='append', dest='blacklisted_rules',
        help="A rule to blacklist (remove from the results.)  You can specify more than one of these options.")
    parser.add_argument('-B', '--blacklist-path', required=False, default=None, dest='blacklisted_rules_path',
        help="Path to a file that contains a list of rules to blacklist, one per line.")

    parser.add_argument('-d', '--signature-dir', dest='signature_dir', default=None,
        help="DEPRECATED: Use a different signature directory than the default.")

    args = parser.parse_args()

    if len(args.yara_rules) == 0 and len(args.yara_dirs) == 0 and len(args.yara_repos) == 0 and args.signature_dir is None:
        args.signature_dir = '/opt/signatures'

    logging.basicConfig(level=logging.DEBUG if args.log_debug else logging.INFO)

    # load any blacklisting
    if args.blacklisted_rules_path is not None:
        with open(args.blacklisted_rules_path, 'r') as fp:
            args.blacklisted_rules.extend([x.strip() for x in fp.read().split('\n')])

    scanner = YaraScanner(signature_dir=args.signature_dir, test_mode=args.test)
    scanner.blacklist = args.blacklisted_rules
    for file_path in args.yara_rules:
        scanner.track_yara_file(file_path)

    for dir_path in args.yara_dirs:
        scanner.track_yara_dir(dir_path)

    for repo_path in args.yara_repos:
        scanner.track_yara_repository(repo_path)

    scanner.load_rules()

    if scanner.check_rules():
        scanner.load_rules()

    if args.compile_only or args.test:
        sys.exit(0)

    exit_result = 0

    def scan_file(file_path):
        global exit_result
        try:
            if scanner.scan(file_path):
                if args.dump_json:
                    json.dump(scanner.scan_results, sys.stdout, sort_keys=True, indent=4, cls=YaraJSONEncoder)
                else:
                    print(file_path)
                    for match in scanner.scan_results:
                        print('\t{}'.format(match['rule']))
        except Exception as e:
            log.error("scan failed for {}: {}".format(file_path, e))
            exit_result = 1

    def scan_dir(dir_path):
        for file_path in os.listdir(dir_path):
            file_path = os.path.join(dir_path, file_path)
            if os.path.isdir(file_path):
                if args.recursive:
                    scan_dir(file_path)
            else:
                scan_file(file_path)

    if args.from_stdin:
        for line in sys.stdin:
            line = line.strip()
            scan_file(line)
    else:
        for path in args.paths:
            if os.path.isdir(path):
                scan_dir(path)
            else:
                scan_file(path)

    sys.exit(exit_result)

if __name__ == '__main__':
    main()
