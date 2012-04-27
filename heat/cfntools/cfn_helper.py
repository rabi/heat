#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Implements cfn metadata handling

Resource metadata currently implemented:
    * config/packages
    * config/services

Not implemented yet:
    * config sets
    * config/sources
    * config/commands
    * config/files
    * config/users
    * config/groups
    * command line args
      - placeholders are ignored
"""

import ConfigParser
import errno
import grp
import json
import logging
import os
import os.path
import pwd
import rpmUtils.updates as rpmupdates
import rpmUtils.miscutils as rpmutils
import subprocess
import sys


def to_boolean(b):
    val = b.lower().strip() if isinstance(b, basestring) else b
    return val in [True, 'true', 'yes', '1', 1]


class HupConfig(object):
    def __init__(self, fp_list):
        self.config = ConfigParser.SafeConfigParser(allow_no_value=True)
        for fp in fp_list:
            self.config.readfp(fp)

        self.load_main_section()

        self.hooks = {}
        for s in self.config.sections():
            if s != 'main':
                self.hooks[s] = Hook(s,
                                     self.config.get(s, 'triggers'),
                                     self.config.get(s, 'path'),
                                     self.config.get(s, 'runas'),
                                     self.config.get(s, 'action'))

    def load_main_section(self):
        # required values
        self.stack = self.config.get('main', 'stack')
        self.credential_file = self.config.get('main', 'credential-file')
        try:
            with open(self.credential_file) as f:
                self.credentials = f.read()
        except:
            raise Exception("invalid credentials file %s" %
                            self.credential_file)

        # optional values
        try:
            self.region = self.config.get('main', 'region')
        except ConfigParser.NoOptionError:
            self.region = 'nova'

        try:
            self.interval = self.config.getint('main', 'interval')
        except ConfigParser.NoOptionError:
            self.interval = 10

    def __str__(self):
        return '{stack: %s, credential_file: %s, region: %s, interval:%d}' % \
            (self.stack, self.credential_file, self.region, self.interval)

    def unique_resources_get(self):
        resources = []
        for h in self.hooks:
            r = self.hooks[h].resource_name_get()
            if not r in resources:
                resources.append(self.hooks[h].resource_name_get())
        return resources


class Hook(object):
    def __init__(self, name, triggers, path, runas, action):
        self.name = name
        self.triggers = triggers
        self.path = path
        self.runas = runas
        self.action = action

    def resource_name_get(self):
        sp = self.path.split('.')
        return sp[1]

    def event(self, ev_name, ev_object, ev_resource):
        if self.resource_name_get() == ev_resource and \
           ev_name in self.triggers:
            CommandRunner(self.action).run(user=self.runas)
        else:
            logging.debug('event: {%s, %s, %s} did not match %s' % \
                          (ev_name, ev_object, ev_resource, self.__str__()))

    def __str__(self):
        return '{%s, %s, %s, %s, %s}' % \
            (self.name,
             self.triggers,
             self.path,
             self.runas,
             self.action)


class CommandRunner(object):
    """
    Helper class to run a command and store the output.
    """

    def __init__(self, command):
        self._command = command
        self._stdout = None
        self._stderr = None
        self._status = None

    def __str__(self):
        s = "CommandRunner:"
        s += "\n\tcommand: %s" % self._command
        if self._status:
            s += "\n\tstatus: %s" % self._status
        if self._stdout:
            s += "\n\tstdout: %s" % self._stdout
        if self._stderr:
            s += "\n\tstderr: %s" % self._stderr
        return s

    def run(self, user='root'):
        """
        Run the Command and return the output.

        Returns:
            self
        """
        logging.debug("Running command: %s" % self._command)
        cmd = ['su', user, '-c', self._command]
        subproc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        output = subproc.communicate()

        self._status = subproc.returncode
        self._stdout = output[0]
        self._stderr = output[1]

        return self

    @property
    def stdout(self):
        return self._stdout

    @property
    def stderr(self):
        return self._stderr

    @property
    def status(self):
        return self._status


class RpmHelper(object):

    _rpm_util = rpmupdates.Updates([], [])

    @classmethod
    def prepcache(cls):
        """
        Prepare the yum cache
        """
        CommandRunner("yum -y makecache").run()

    @classmethod
    def compare_rpm_versions(cls, v1, v2):
        """
        Compare two RPM version strings.

        Arguments:
            v1 -- a version string
            v2 -- a version string

        Returns:
            0 -- the versions are equal
            1 -- v1 is greater
           -1 -- v2 is greater
        """
        if v1 and v2:
            return rpmutils.compareVerOnly(v1, v2)
        elif v1:
            return 1
        elif v2:
            return -1
        else:
            return 0

    @classmethod
    def newest_rpm_version(cls, versions):
        """
        Returns the highest (newest) version from a list of versions.

        Arguments:
            versions -- A list of version strings
                        e.g., ['2.0', '2.2', '2.2-1.fc16', '2.2.22-1.fc16']
        """
        if versions:
            if isinstance(versions, basestring):
                return versions
            versions = sorted(versions, rpmutils.compareVerOnly,
                    reverse=True)
            return versions[0]
        else:
            return None

    @classmethod
    def rpm_package_version(cls, pkg):
        """
        Returns the version of an installed RPM.

        Arguments:
            pkg -- A package name
        """
        cmd = "rpm -q --queryformat '%{VERSION}-%{RELEASE}' %s" % pkg
        command = CommandRunner(cmd).run()
        return command.stdout

    @classmethod
    def rpm_package_installed(cls, pkg):
        """
        Indicates whether pkg is in rpm database.

        Arguments:
            pkg -- A package name (with optional version and release spec).
                   e.g., httpd
                   e.g., httpd-2.2.22
                   e.g., httpd-2.2.22-1.fc16
        """
        command = CommandRunner("rpm -q %s" % pkg).run()
        return command.status == 0

    @classmethod
    def yum_package_available(cls, pkg):
        """
        Indicates whether pkg is available via yum

        Arguments:
            pkg -- A package name (with optional version and release spec).
                   e.g., httpd
                   e.g., httpd-2.2.22
                   e.g., httpd-2.2.22-1.fc16
        """
        cmd_str = "yum -C -y --showduplicates list available %s" % pkg
        command = CommandRunner(cmd_str).run()
        return command.status == 0

    @classmethod
    def install(cls, packages, rpms=True):
        """
        Installs (or upgrades) a set of packages via RPM or via Yum.

        Arguments:
            packages -- a list of packages to install
            rpms     -- if True:
                        * use RPM to install the packages
                        * packages must be a list of URLs to retrieve RPMs
                        if False:
                        * use Yum to install packages
                        * packages is a list of:
                          - pkg name (httpd), or
                          - pkg name with version spec (httpd-2.2.22), or
                          - pkg name with version-release spec (httpd-2.2.22-1.fc16)
        """
        if rpms:
            cmd = "rpm -U --force --nosignature "
            cmd += " ".join(packages)
            logging.info("Installing packages: %s" % cmd)
        else:
            cmd = "yum -y install "
            cmd += " ".join(packages)
            logging.info("Installing packages: %s" % cmd)
        command = CommandRunner(cmd).run()
        if command.status:
            logging.warn("Failed to install packages: %s" % cmd)

    @classmethod
    def downgrade(cls, packages, rpms=True):
        """
        Downgrades a set of packages via RPM or via Yum.

        Arguments:
            packages -- a list of packages to downgrade
            rpms     -- if True:
                        * use RPM to downgrade (replace) the packages
                        * packages must be a list of URLs to retrieve the RPMs
                        if False:
                        * use Yum to downgrade packages
                        * packages is a list of:
                          - pkg name with version spec (httpd-2.2.22), or
                          - pkg name with version-release spec (httpd-2.2.22-1.fc16)
        """
        if rpms:
            cls.install(packages)
        else:
            cmd = "yum -y downgrade "
            cmd += " ".join(packages)
            logging.info("Downgrading packages: %s" % cmd)
            command = Command(cmd).run()
            if command.status:
                logging.warn("Failed to downgrade packages: %s" % cmd)


class PackagesHandler(object):
    _packages = {}

    _package_order = ["dpkg", "rpm", "apt", "yum"]

    @staticmethod
    def _pkgsort(pkg1, pkg2):
        order = PackagesHandler._package_order
        p1_name = pkg1[0]
        p2_name = pkg2[0]
        if p1_name in order and p2_name in order:
            return cmp(order.index(p1_name), order.index(p2_name))
        elif p1_name in order:
            return -1
        elif p2_name in order:
            return 1
        else:
            return cmp(p1_name.lower(), p2_name.lower())

    def __init__(self, packages):
        self._packages = packages

    def _handle_gem_packages(self, packages):
        #FIXME: handle rubygems
        pass

    def _handle_python_packages(self, packages):
        #FIXME: handle python easyinstall
        pass

    def _handle_yum_packages(self, packages):
        """
        Handle installation, upgrade, or downgrade of a set of packages via yum.

        Arguments:
        packages -- a package entries map of the form:
                      "pkg_name" : "version",
                      "pkg_name" : ["v1", "v2"],
                      "pkg_name" : []

        For each package entry:
          * if no version is supplied and the package is already installed, do
            nothing
          * if no version is supplied and the package is _not_ already
            installed, install it
          * if a version string is supplied, and the package is already
            installed, determine whether to downgrade or upgrade (or do nothing
            if version matches installed package)
          * if a version array is supplied, choose the highest version from the
            array and follow same logic for version string above
        """
        # collect pkgs for batch processing at end
        installs = []
        downgrades = []
        # update yum cache
        RpmHelper.prepcache()
        for pkg_name, versions in packages.iteritems():
            ver = RpmHelper.newest_rpm_version(versions)
            pkg = "%s-%s" % (pkg_name, ver) if ver else pkg_name
            if RpmHelper.rpm_package_installed(pkg):
                # FIXME:print non-error, but skipping pkg
                pass
            elif not RpmHelper.yum_package_available(pkg):
                logging.warn("Skipping package '%s'. Not available via yum" % pkg)
            elif not ver:
                installs.append(pkg)
            else:
                current_ver = RpmHelper.rpm_package_version(pkg)
                rc = RpmHelper.compare_rpm_versions(current_ver, ver)
                if rc < 0:
                    installs.append(pkg)
                elif rc > 0:
                    downgrades.append(pkg)
            if installs:
                RpmHelper.install(installs, rpms=False)
            if downgrades:
                RpmHelper.downgrade(downgrades)

    def _handle_rpm_packages(sef, packages):
        """
        Handle installation, upgrade, or downgrade of a set of packages via rpm.

        Arguments:
        packages -- a package entries map of the form:
                      "pkg_name" : "url"

        For each package entry:
          * if the EXACT package is already installed, skip it
          * if a different version of the package is installed, overwrite it
          * if the package isn't installed, install it
        """
        #FIXME: handle rpm installs
        pass

    def _handle_apt_packages(self, packages):
        #FIXME: handle apt-get
        pass

    # map of function pionters to handle different package managers
    _package_handlers = {
            "yum": _handle_yum_packages,
            "rpm": _handle_rpm_packages,
            "apt": _handle_apt_packages,
            "rubygems": _handle_gem_packages,
            "python": _handle_python_packages
    }

    def _package_handler(self, manager_name):
        handler = None
        if manager_name in self._package_handlers:
            handler = self._package_handlers[manager_name]
        return handler

    def apply_packages(self):
        """
        Install, upgrade, or downgrade packages listed
        Each package is a dict containing package name and a list of versions
        Install order:
          * dpkg
          * rpm
          * apt
          * yum
        """
        if not self._packages:
            return
        packages = sorted(self._packages.iteritems(), PackagesHandler._pkgsort)

        for manager, package_entries in packages:
            handler = self._package_handler(manager)
            if not handler:
                logging.warn("Skipping invalid package type: %s" % manager)
            else:
                handler(self, package_entries)

class FilesHandler(object):
    def __init__(self, files):
        self._files = files

    def apply_files(self):
        if not self._files:
            return
        for fdest, meta in self._files.iteritems():
            dest = fdest.encode()
            try:
                os.makedirs(os.path.dirname(dest))
            except OSError as e:
                if e.errno == errno.EEXIST:
                    logging.debug(str(e))
                else:
                    logging.exception(e)

            if 'content' in meta:
                if isinstance(meta['content'], basestring):
                    f = open(dest, 'w')
                    f.write(meta['content'])
                    f.close()
                else:
                    f = open(dest, 'w')
                    f.write(json.dumps(meta['content'], indent=4))
                    f.close()
            elif 'source' in meta:
                CommandRunner('wget -O %s %s' % (dest, meta['source'])).run()
            else:
                logging.error('%s %s' % (dest, str(meta)))
                continue

            uid = -1
            gid = -1
            if 'owner' in meta:
                try:
                    user_info = pwd.getpwnam(meta['owner'])
                    uid = user_info[2]
                except KeyError as ex:
                    pass

            if 'group' in meta:
                try:
                    group_info = grp.getgrnam(meta['group'])
                    gid = group_info[2]
                except KeyError as ex:
                    pass

            os.chown(dest, uid, gid)
            if 'mode' in meta:
                os.chmod(dest, int(meta['mode'], 8))


class ServicesHandler(object):
    _services = {}

    def __init__(self, services, resource=None, hooks=None):
        self._services = services
        self.resource = resource
        self.hooks = hooks

    def _handle_sysv_command(self, service, command):
        service_exe = "/sbin/service"
        enable_exe = "/sbin/chkconfig"
        cmd = ""
        if "enable" == command:
            cmd = "%s %s on" % (enable_exe, service)
        elif "disable" == command:
            cmd = "%s %s off" % (enable_exe, service)
        elif "start" == command:
            cmd = "%s %s start" % (service_exe, service)
        elif "stop" == command:
            cmd = "%s %s stop" % (service_exe, service)
        elif "status" == command:
            cmd = "%s %s status" % (service_exe, service)
        command = CommandRunner(cmd)
        command.run()
        return command

    def _handle_systemd_command(self, service, command):
        exe = "/bin/systemctl"
        cmd = ""
        service = '%s.service' % service
        if "enable" == command:
            cmd = "%s enable %s" % (exe, service)
        elif "disable" == command:
            cmd = "%s disable %s" % (exe, service)
        elif "start" == command:
            cmd = "%s start %s" % (exe, service)
        elif "stop" == command:
            cmd = "%s stop %s" % (exe, service)
        elif "status" == command:
            cmd = "%s status %s" % (exe, service)
        command = CommandRunner(cmd)
        command.run()
        return command

    def _initialize_service(self, handler, service, properties):
        if "enabled" in properties:
            enable = to_boolean(properties["enabled"])
            if enable:
                logging.info("Enabling service %s" % service)
                handler(self, service, "enable")
            else:
                logging.info("Disabling service %s" % service)
                handler(self, service, "disable")

        if "ensureRunning" in properties:
            ensure_running = to_boolean(properties["ensureRunning"])
            command = handler(self, service, "status")
            running = command.status == 0
            if ensure_running and not running:
                logging.info("Starting service %s" % service)
                handler(self, service, "start")
            elif not ensure_running and running:
                logging.info("Stopping service %s" % service)
                handler(self, service, "stop")

    def _monitor_service(self, handler, service, properties):
        if "ensureRunning" in properties:
            ensure_running = to_boolean(properties["ensureRunning"])
            command = handler(self, service, "status")
            running = command.status == 0
            if ensure_running and not running:
                logging.info("Restarting service %s" % service)
                start_cmd = handler(self, service, "start")
                if start_cmd.status != 0:
                    logging.warning('Service %s did not start. STDERR: %s' %
                                    (service, start_cmd.stderr))
                    return
                for h in self.hooks:
                    self.hooks[h].event('service.restarted',
                                        service, self.resource)

    def _monitor_services(self, handler, services):
        for service, properties in services.iteritems():
            self._monitor_service(handler, service, properties)

    def _initialize_services(self, handler, services):
        for service, properties in services.iteritems():
            self._initialize_service(handler, service, properties)

    # map of function pointers to various service handlers
    _service_handlers = {
        "sysvinit": _handle_sysv_command,
        "systemd": _handle_systemd_command
    }

    def _service_handler(self, manager_name):
        handler = None
        if manager_name in self._service_handlers:
            handler = self._service_handlers[manager_name]
        return handler

    def apply_services(self):
        """
        Starts, stops, enables, disables services
        """
        if not self._services:
            return
        for manager, service_entries in self._services.iteritems():
            handler = self._service_handler(manager)
            if not handler:
                logging.warn("Skipping invalid service type: %s" % manager)
            else:
                self._initialize_services(handler, service_entries)

    def monitor_services(self):
        """
        Restarts failed services, and runs hooks.
        """
        if not self._services:
            return
        for manager, service_entries in self._services.iteritems():
            handler = self._service_handler(manager)
            if not handler:
                logging.warn("Skipping invalid service type: %s" % manager)
            else:
                self._monitor_services(handler, service_entries)


class Metadata(object):
    _metadata = None
    _init_key = "AWS::CloudFormation::Init"

    def __init__(self, stack, resource, access_key=None,
                 secret_key=None, credentials_file=None, region=None):

        self.stack = stack
        self.resource = resource
        self.access_key = access_key
        self.secret_key = secret_key
        self.credentials_file = credentials_file
        self.region = region

        # TODO(asalkeld) is this metadata for the local resource?
        self._is_local_metadata = True
        self._metadata = None

    def retrieve(self, meta_str=None):
        """
        Read the metadata from the given filename
        """
        if meta_str:
            self._data = meta_str
        else:
            f = open("/var/lib/cloud/data/cfn-init-data")
            self._data = f.read()
            f.close()

        if isinstance(self._data, str):
            self._metadata = json.loads(self._data)
        else:
            self._metadata = self._data

    def __str__(self):
        return self._metadata

    def _is_valid_metadata(self):
        """
        Should find the AWS::CloudFormation::Init json key
        """
        is_valid = self._metadata and \
                   self._init_key in self._metadata and \
                   self._metadata[self._init_key]
        if is_valid:
            self._metadata = self._metadata[self._init_key]
        return is_valid

    def _process_config(self):
        """
        Parse and process a config section
          * packages
          * sources (not yet)
          * users (not yet)
          * groups (not yet)
          * files
          * commands (not yet)
          * services
        """

        self._config = self._metadata["config"]
        PackagesHandler(self._config.get("packages")).apply_packages()
        #FIXME: handle sources
        #FIXME: handle users
        #FIXME: handle groups
        FilesHandler(self._config.get("files")).apply_files()
        #FIXME: handle commands
        ServicesHandler(self._config.get("services")).apply_services()

    def cfn_init(self):
        """
        Process the resource metadata
        """
        # FIXME: when config sets are implemented, this should select the correct
        # config set from the metadata, and send each config in the config set to
        # process_config
        if not self._is_valid_metadata():
            raise Exception("invalid metadata")
        else:
            self._process_config()

    def cfn_hup(self, hooks):
        """
        Process the resource metadata
        """
        if not self._is_valid_metadata():
            raise Exception("invalid metadata")
        else:
            if self._is_local_metadata:
                self._config = self._metadata["config"]
                s = self._config.get("services")
                sh = ServicesHandler(s, resource=self.resource, hooks=hooks)
                sh.monitor_services()
