import os
import sys
import time

from cf_remote.remote import get_info, print_info, install_host, uninstall_host, run_command, transfer_file, deploy_masterfiles
from cf_remote.packages import Releases
from cf_remote.web import download_package
from cf_remote.paths import cf_remote_dir, CLOUD_CONFIG_FPATH, CLOUD_STATE_FPATH
from cf_remote.utils import save_file, strip_user, read_json, write_json, whoami
from cf_remote.spawn import VM, VMRequest, Providers, AWSCredentials
from cf_remote.spawn import spawn_vms, destroy_vms, dump_vms_info, get_cloud_driver
from cf_remote import log
from cf_remote import cloud_data

def info(hosts, users=None):
    assert hosts
    log.debug("hosts='{}'".format(hosts))
    errors = 0
    for host in hosts:
        data = get_info(host, users=users)
        if data:
            print_info(data)
        else:
            errors += 1
    return errors


def run(hosts, command, users=None, sudo=False, raw=False):
    assert hosts
    errors = 0
    for host in hosts:
        lines = run_command(host=host, command=command, users=users, sudo=sudo)
        if lines is None:
            log.error("Command: '{}'\nFailed on host: '{}'".format(command, host))
            errors += 1
            continue
        host_colon = (host + ":").ljust(16)
        if lines == "":
            if not raw:
                print("{} '{}'".format(host_colon, command))
            continue
        cmd = command
        lines = lines.replace("\r", "")
        for line in lines.split("\n"):
            if raw:
                print(line)
            elif cmd:
                print("{} '{}' -> '{}'".format(host_colon, cmd, line))
                fill = " " * (len(cmd) + 7)
                cmd = None
            else:
                print("{}{}'{}'".format(host_colon, fill, line))
    return errors


def sudo(hosts, command, users=None, raw=False):
    return run(hosts, command, users, sudo=True, raw=raw)


def scp(hosts, files, users=None):
    errors = 0
    for host in hosts:
        for file in files:
            errors += transfer_file(host, file, users)
    return errors


def install(
        hubs,
        clients,
        *,
        bootstrap=None,
        package=None,
        hub_package=None,
        client_package=None,
        version=None,
        demo=False,
        call_collect=False,
        edition=None):
    assert hubs or clients
    assert not (hubs and clients and package)
    # These assertions are checked in main.py

    if not hub_package:
        hub_package = package
    if not client_package:
        client_package = package
    if bootstrap:
        if type(bootstrap) is str:
            bootstrap = [bootstrap]
        save_file(os.path.join(cf_remote_dir(), "policy_server.dat"), "\n".join(bootstrap + [""]))
    errors = 0
    if hubs:
        if type(hubs) is str:
            hubs = [hubs]
        for index, hub in enumerate(hubs):
            log.debug("Installing {} hub package on '{}'".format(edition, hub))
            errors += install_host(
                hub,
                hub=True,
                package=hub_package,
                bootstrap=bootstrap[index % len(bootstrap)] if bootstrap else None,
                version=version,
                demo=demo,
                call_collect=call_collect,
                edition=edition)
    for index, host in enumerate(clients or []):
        log.debug("Installing {} client package on '{}'".format(edition, host))
        errors += install_host(
            host,
            hub=False,
            package=client_package,
            bootstrap=bootstrap[index % len(bootstrap)] if bootstrap else None,
            version=version,
            demo=demo,
            edition=edition)
    if demo and hubs:
        for hub in hubs:
            print(
                "Your demo hub is ready: https://{}/ (Username: admin, Password: password)".
                format(strip_user(hub)))
    return errors

def _iterate_over_packages(tags=None, version=None, edition=None, download=False):
    releases = Releases(edition)
    print("Available releases: {}".format(releases))

    release = releases.default
    if version:
        release = releases.pick_version(version)
    print("Using {}:".format(release))
    artifacts = release.find(tags)

    if len(artifacts) == 0:
        print("No suitable packages found")
    else:
        for artifact in artifacts:
            if download:
                download_package(artifact.url)
            else:
                print(artifact.url)
    return 0

# named list_command to not conflict with list()
def list_command(tags=None, version=None, edition=None):
    return _iterate_over_packages(tags, version, edition, False)

def download(tags=None, version=None, edition=None):
    return _iterate_over_packages(tags, version, edition, True)

def spawn(platform, count, role, group_name, provider=Providers.AWS, region=None):
    if os.path.exists(CLOUD_CONFIG_FPATH):
        creds_data = read_json(CLOUD_CONFIG_FPATH)
    else:
        print("Cloud credentials not found at %s" % CLOUD_CONFIG_FPATH)
        return 1

    if os.path.exists(CLOUD_STATE_FPATH):
        vms_info = read_json(CLOUD_STATE_FPATH)
    else:
        vms_info = dict()

    group_key = "@%s" % group_name
    if group_key in vms_info:
        print("Group '%s' already exists!" % group_key)
        return 1

    try:
        creds = AWSCredentials(creds_data["aws"]["key"], creds_data["aws"]["secret"])
        sec_groups = creds_data["aws"]["security_groups"]
        key_pair = creds_data["aws"]["key_pair"]
    except KeyError:
        print("Incomplete AWS credential info") # TODO: report missing keys
        return 1

    region = region or creds_data["aws"].get("region", "eu-west-1")

    requests = []
    for i in range(count):
        vm_name = whoami()[0:2] + group_name + "-" + platform + role + str(i)
        requests.append(VMRequest(platform=platform,
                                  name=vm_name,
                                  size=None))
    print("Spawning VMs...", end="")
    sys.stdout.flush()
    vms = spawn_vms(requests, creds, region, key_pair,
                    security_groups=sec_groups,
                    provider=provider,
                    role=role)
    print("DONE")

    if not all(vm.public_ips for vm in vms):
        print("Waiting for VMs to get IP addresses...", end="")
        sys.stdout.flush()      # STDOUT is line-buffered
        while not all(vm.public_ips for vm in vms):
            time.sleep(1)
            print(".", end="")
            sys.stdout.flush()      # STDOUT is line-buffered
        print("DONE")

    vms_info[group_key] = dump_vms_info(vms)

    write_json(CLOUD_STATE_FPATH, vms_info)
    print("Details about the spawned VMs can be found in %s" % CLOUD_STATE_FPATH)

    return 0

def destroy(group_name=None):
    if os.path.exists(CLOUD_CONFIG_FPATH):
        creds_data = read_json(CLOUD_CONFIG_FPATH)
    else:
        print("Cloud credentials not found at %s" % CLOUD_CONFIG_FPATH)
        return 1

    creds = AWSCredentials(creds_data["aws"]["key"], creds_data["aws"]["secret"])

    if not os.path.exists(CLOUD_STATE_FPATH):
        print("No saved cloud state info")
        return 1

    vms_info = read_json(CLOUD_STATE_FPATH)

    to_destroy = []
    if group_name:
        print("Destroying hosts in the '%s' group" % group_name)
        if not group_name.startswith("@"):
            group_name = "@" + group_name
        if group_name not in vms_info:
            print("Group '%s' not found" % group_name)
            return 1

        region = vms_info[group_name]["meta"]["region"]
        driver = get_cloud_driver(Providers.AWS, creds, region)
        for name, vm_info in vms_info[group_name].items():
            if name == "meta":
                continue
            vm_uuid = vm_info["uuid"]
            vm = VM.get_by_uuid(vm_uuid, driver)
            if vm is not None:
                to_destroy.append(vm)
            else:
                print("VM '%s' not found in the clouds" % vm_uuid)
        del vms_info[group_name]
    else:
        print("Destroying all hosts")
        for group_name in [key for key in vms_info.keys() if key.startswith("@")]:
            region = vms_info[group_name]["meta"]["region"]
            driver = get_cloud_driver(Providers.AWS, creds, region)
            for name, vm_info in vms_info[group_name].items():
                if name == "meta":
                    continue
                vm_uuid = vm_info["uuid"]
                vm = VM.get_by_uuid(vm_uuid, driver)
                if vm is not None:
                    to_destroy.append(vm)
                else:
                    print("VM '%s' not found in the clouds" % vm_uuid)
            del vms_info[group_name]

    destroy_vms(to_destroy)
    write_json(CLOUD_STATE_FPATH, vms_info)
    return 0

def list_platforms():
    print("Available platforms:")
    for key in sorted(cloud_data.aws_platforms.keys()):
        print(key)
    return 0

def init_cloud_config():
    if os.path.exists(CLOUD_CONFIG_FPATH):
        print("File %s already exists" % CLOUD_CONFIG_FPATH)
        return 1
    empty_config = {
        "aws": {
            "key": "TBD",
            "secret": "TBD",
            "key_pair": "TBD",
            "security_groups": ["TBD"],
            "region": "OPTIONAL (DEFAULT: eu-west-1)",
        },
    }
    write_json(CLOUD_CONFIG_FPATH, empty_config)
    print("Config file %s created, please complete the configuration in it." % CLOUD_CONFIG_FPATH)
    return 0

def uninstall(hosts):
    errors = 0
    for host in hosts:
        errors += uninstall_host(host)
    return errors

def deploy(hubs, directory):
    assert(directory.endswith("/masterfiles"))
    assert(os.path.isfile(directory + "/autogen.sh"))
    os.system(f"bash -c 'cd {directory} && ./autogen.sh 1>/dev/null 2>&1'")
    assert(os.path.isfile(directory + "/promises.cf"))

    assert(not cf_remote_dir().endswith("/"))
    tarball = cf_remote_dir() + "/masterfiles.tgz"
    above = directory[0:-len("/masterfiles")]
    os.system(f"rm -rf {tarball}")
    os.system(f"tar -czf {tarball} -C {above} masterfiles")
    errors = 0
    for hub in hubs:
        errors += deploy_masterfiles(hub, tarball)
    return errors
