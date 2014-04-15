import getpass
import logging
import shutil
import tempfile
import time
import psutil
from itertools import chain
import yaml
from subprocess import Popen, PIPE
from utils import wait_until_true, run_once
import simplejson as json

from minion_sim.sim import MinionSim
from calamari_common.config import CalamariConfig

config = CalamariConfig()
logging.basicConfig()

log = logging.getLogger(__name__)

class CephControl(object):
    """
    Interface for tests to control one or more Ceph clusters under test.

    This can either be controlling the minion-sim, running unprivileged
    in a development environment, or it can be controlling a real life
    Ceph cluster.

    Some configuration arguments may be interpreted by a
    dev implementation as a "simulate this", while a real-cluster
    implementation might interpret them as "I require this state, skip
    the test if this cluster can't handle that".
    """

    def configure(self, server_count, cluster_count=1):
        """
        Tell me about the kind of system you would like.

        We will give you that system in a clean state or not at all:
        - Sometimes by setting it up for you here and now
        - Sometimes by cleaning up an existing cluster that's left from a previous test
        - Sometimes a clean cluster is already present for us
        - Sometimes we may not be able to give you the configuration you asked for
          (maybe you asked for more servers than we have servers) and have to
          throw you a test skip exception
        - Sometimes we may have a cluster that we can't clean up well enough
          to hand back to you, and have to throw you an error exception
        """
        raise NotImplementedError()

    def shutdown(self):
        """
        This cluster will not be used further by the test.

        If you created a cluster just for the test, tear it down here.  If the
        cluster was already up, just stop talking to it.
        """
        raise NotImplementedError()

    def mark_osd_in(self, fsid, osd_id, osd_in=True):
        raise NotImplementedError()

    def get_server_fqdns(self):
        raise NotImplementedError()

    def go_dark(self, fsid, dark=True, minion_id=None):
        """
        Create the condition where network connectivity between
        the calamari server and the ceph cluster is lost.
        """
        pass

    def get_fqdns(self, fsid):
        """
        Return all the FQDNs of machines with salt minion
        """
        raise NotImplementedError()


class EmbeddedCephControl(CephControl):
    """
    One or more simulated ceph clusters
    """
    def __init__(self):
        self._config_dirs = {}
        self._sims = {}

    def configure(self, server_count, cluster_count=1):
        osds_per_host = 4

        for i in range(0, cluster_count):
            domain = "cluster%d.com" % i
            config_dir = tempfile.mkdtemp()
            sim = MinionSim(config_dir, server_count, osds_per_host, port=8761 + i, domain=domain)
            fsid = sim.cluster.fsid
            self._config_dirs[fsid] = config_dir
            self._sims[fsid] = sim
            sim.start()

    def shutdown(self):
        log.info("%s.shutdown" % self.__class__.__name__)

        for sim in self._sims.values():
            sim.stop()
            sim.join()

        log.debug("lingering processes: %s" %
                  [p.name for p in psutil.process_iter() if p.username == getpass.getuser()])
        # Sleeps in tests suck... this one is here because the salt minion doesn't give us a nice way
        # to ensure that when we shut it down, subprocesses are complete before it returns, and even
        # so we can't be sure that messages from a dead minion aren't still winding their way
        # to cthulhu after this point.  So we fudge it.
        time.sleep(5)

        for config_dir in self._config_dirs.values():
            shutil.rmtree(config_dir)

    def get_server_fqdns(self):
        return list(chain(*[s.get_minion_fqdns() for s in self._sims.values()]))

    def mark_osd_in(self, fsid, osd_id, osd_in=True):
        self._sims[fsid].cluster.set_osd_state(osd_id, osd_in=1 if osd_in else 0)

    def go_dark(self, fsid, dark=True, minion_id=None):
        if minion_id:
            if dark:
                self._sims[fsid].halt_minion(minion_id)
            else:
                self._sims[fsid].start_minion(minion_id)
        else:
            if dark:
                self._sims[fsid].halt_minions()
            else:
                self._sims[fsid].start_minions()

        # Sleeps in tests suck... this one is here because the salt minion doesn't give us a nice way
        # to ensure that when we shut it down, subprocesses are complete before it returns, and even
        # so we can't be sure that messages from a dead minion aren't still winding their way
        # to cthulhu after this point.  So we fudge it.
        time.sleep(5)

    def get_fqdns(self, fsid):
        return self._sims[fsid].get_minion_fqdns()

    def get_service_fqdns(self, fsid, service_type):
        return self._sims[fsid].cluster.get_service_fqdns(service_type)


class ExternalCephControl(CephControl):
    """
    This is the code that talks to a cluster. It is currently dependent on teuthology
    """

    def __init__(self):
        with open(config.get('testing', 'external_cluster_path')) as f:
            self.config = yaml.load(f)

        # TODO parse this out of the cluster.yaml
        self.cluster_name = 'ceph'

    def _run_command(self, target, command):
        ssh_command = 'ssh ubuntu@{target} {command}'.format(target=target, command=command)
        proc = Popen(ssh_command, shell=True, stdout=PIPE)
        return proc.communicate()[0]

    def configure(self, server_count, cluster_count=1):

        # I hope you only wanted three, because I ain't buying
        # any more servers...
        # TODO raise skip tests if these are different
        assert server_count == 3
        assert cluster_count == 1

        # TODO parse fsid out of cluster.yaml
        fsid = 12345
        target = self._get_admin_node(fsid=fsid)
        # Ensure all OSDs are initially up: assertion per #7813
        self._wait_for_state(fsid,
                             lambda: self._run_command(target, "ceph --cluster {cluster} osd stat -f json-pretty".format(cluster=self.cluster_name)),
                             self._check_osd_up_and_in)

        # Ensure there are initially no pools but the default ones. assertion per #7813
        self._wait_for_state(fsid,
                             lambda: self._run_command(target, "ceph --cluster {cluster} osd lspools -f json-pretty".format(cluster=self.cluster_name)),
                             self._check_default_pools_only)

        # wait till all PGs are active and clean assertion per #7813
        # TODO stop scraping this, defer this because pg stat -f json-pretty is anything but
        self._wait_for_state(fsid,
                             lambda: self._run_command(target, "ceph --cluster {cluster} pg stat".format(cluster=self.cluster_name)),
                             self._check_pgs_active_and_clean)

        self._bootstrap(12345, self.config['master_fqdn'])

    def get_server_fqdns(self):
        return [target.split('@')[1] for target in self.config['cluster'].iterkeys()]

    def get_service_fqdns(self, fsid, service_type):
        # I run OSDs and mons in the same places (on all three servers)
        return self.get_server_fqdns()

    def shutdown(self):
        pass

    def get_fqdns(self, fsid):
        # TODO when we support multiple cluster change this
        return self.get_server_fqdns()

    def go_dark(self, fsid, dark=True, minion_id=None):
        action = dark and 'stop' or 'start'
        for target in self.get_fqdns(fsid):
            if minion_id and minion_id not in target:
                continue
            output = self._run_command(target, "sudo service salt-minion {action}".format(action=action))

    def _check_default_pools_only(self, output):
        try:
            pools = json.loads(output)
            return {'data', 'metadata', 'rbd'} == set([x['poolname'] for x in pools])
        except ValueError:
            log.warning('Failed to parse osd lspools output')

        return False

    def _wait_for_state(self, fsid, command, state):
        log.info('Waiting for {state} on cluster {fsid}'.format(state=state, fsid=fsid))
        wait_until_true(lambda: state(command()))

    def _check_pgs_active_and_clean(self, output):
        if output:
            try:
                _, total_stat, pg_stat, _ = output.replace(';', ':').split(':')
                return 'active+clean' == pg_stat.split()[1] and total_stat.split()[0] == pg_stat.split()[0]
            except ValueError:
                log.warning('ceph pg stat format may have changed')

        return False

    def _check_osd_up_and_in(self, output):
        try:
            osd_stat = json.loads(output)
            # osd_stat['num_in_osds'] is a string fixed in http://tracker.ceph.com/issues/7159
            return osd_stat['num_osds'] == osd_stat['num_up_osds'] == int(osd_stat['num_in_osds'])
        except ValueError:
            log.warning('Failed to parse osd stat output')

        return False

    @run_once
    def _bootstrap(self, fsid, master_fqdn):
        for target in self.get_fqdns(fsid):
            log.info('Bootstrapping salt-minion on {target}'.format(target=target))

            # TODO abstract out the port number
            output = self._run_command(target, '''"wget -O - http://{fqdn}:8000/bootstrap |\
             sudo python ; sudo sed -i 's/^[#]*master:.*$/master: {fqdn}/;s/^[#]*open:.*$/open: True/' /etc/salt/minion && sudo service salt-minion restart"'''.format(fqdn=master_fqdn))
            log.info(output)

    def _get_admin_node(self, fsid):
        for target, roles in self.config['cluster'].iteritems():
            if 'client.0' in roles:
                return target.split('@')[1]

    def mark_osd_in(self, fsid, osd_id, osd_in=True):
        command = osd_in and 'in' or 'out'
        output = self._run_command(self._get_admin_node(fsid), "ceph --cluster {cluster} osd {command} {id}".format(cluster=self.cluster_name, command=command, id=int(osd_id)))
        log.info(output)


if __name__ == "__main__":
    externalctl = ExternalCephControl()
    assert isinstance(externalctl.config, dict)
    externalctl.configure(3)
    # bootstrap salt minions on cluster
    externalctl._bootstrap(12345, externalctl.config['master_fqdn'])


