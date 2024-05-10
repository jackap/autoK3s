from pydantic import BaseModel, IPvAnyAddress, StringConstraints, constr, ValidationError
from typing import Annotated, List
import yaml
import sys
import paramiko
import os
import subprocess

def start_ssh(ssh_key,ip,username) -> paramiko.SSHClient:
    basepath = os.path.expanduser("~")
    sshcon = paramiko.SSHClient()  # will create the object
    sshcon.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # no known_hosts error
    sshcon.connect(str(ip), username=username, key_filename=basepath + ssh_key)  # no passwd needed
    return sshcon


class Config(BaseModel):
    ssh_key: str
    username: str
    master_ip: IPvAnyAddress
    workers: List[IPvAnyAddress]
    cni: Annotated[str, StringConstraints(min_length=1)]


def read_config_file(file_path):
    with open(file_path, 'r') as file:
        config_data = yaml.safe_load(file)
    return config_data


def get_necessary_files(sshcon: paramiko.SSHClient,master_ip):
    print("Copy Kubernetes configuration")
    stdin, stdout, stderr = sshcon.exec_command(
        'sudo cat /etc/rancher/k3s/k3s.yaml')
    stdin.close()
    conf = stdout.read().decode("utf-8").replace("127.0.0.1",str(master_ip))
    with open("./k3s.yaml",'w') as fp:
        fp.write(conf)
        fp.close()

    print("Get node token")
    stdin, stdout, stderr = sshcon.exec_command(
        'sudo cat /var/lib/rancher/k3s/server/node-token')
    stdin.close()
    token = stdout.read().decode("utf-8")[:-1]
    return token



def bootstrap_cilium_master(ssh_key,master_ip,username,) -> str:
    sshcon = start_ssh(ssh_key,master_ip,username)
    opts = "--flannel-backend=none --disable-network-policy "
    print("Installing K3s on master node")
    stdin, stdout, stderr = sshcon.exec_command(
        f'curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="{opts}--cluster-cidr=192.168.0.0/16" sh -')
    stdin.close()
    print(stdout.read().decode("utf-8"))
   
    return get_necessary_files(sshcon,master_ip)



def bootstrap_master(ssh_key,master_ip,username,with_default_cni=False) -> str:
    sshcon = start_ssh(ssh_key,master_ip,username)
    opts = "--flannel-backend=none --disable-network-policy --disable=traefik " if with_default_cni else ""
    print("Installing K3s on master node")
    stdin, stdout, stderr = sshcon.exec_command(
        f'curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="{opts}--cluster-cidr=192.168.0.0/16" sh -')
    stdin.close()
    print(stdout.read().decode("utf-8"))
   
    return get_necessary_files(sshcon,master_ip)


def bootstrap_worker(ssh_key,ip,username,master_ip,token):
    sshcon = start_ssh(ssh_key,ip,username)
    print(f"Bootstrap worker @{ip}")
    stdin, stdout, stderr = sshcon.exec_command(
        f'curl -sfL https://get.k3s.io | K3S_URL=https://{master_ip}:6443 K3S_TOKEN={token} sh -')
    stdin.close()
    print(stdout.read().decode("utf-8"))


def uninstall_worker(ssh_key,ip,username):
    sshcon = start_ssh(ssh_key,ip,username)
    print(f"Uninstalling worker @{ip}")
    stdin, stdout, stderr = sshcon.exec_command(
        f'/usr/local/bin/k3s-agent-uninstall.sh')
    stdin.close()
    print(stdout.read().decode("utf-8"))


def uninstall_master(ssh_key,ip,username):
    sshcon = start_ssh(ssh_key,ip,username)
    print(f"Uninstalling master @{ip}")
    stdin, stdout, stderr = sshcon.exec_command(
        f'/usr/local/bin/k3s-uninstall.sh')
    stdin.close()
    print(stdout.read().decode("utf-8"))


def uninstall(config: Config):
    for worker in config.workers:
        uninstall_worker(config.ssh_key,worker,config.username)
    uninstall_master(config.ssh_key,config.master_ip,config.username)

def install_with_default(config: Config):
    token = bootstrap_master(config.ssh_key,config.master_ip,config.username,True)
    for worker in config.workers:
        bootstrap_worker(config.ssh_key,worker,config.username,config.master_ip,token)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python script.py <config_file>")
        sys.exit(1)

    config_file = sys.argv[1]
    config_data = read_config_file(config_file)

    try:
        config = Config(**config_data)
    except ValidationError as e:
        print(f"Error: Invalid configuration - {e}")

    try:
        uninstall(config)
    except:
        pass
    
    if config.cni == "default":
        print("Install with default K3s configuration")
        install_with_default(config)
        exit(0)
 

    elif config.cni == "calico":
        token = bootstrap_master(config.ssh_key,config.master_ip,config.username)

        for worker in config.workers:
            bootstrap_worker(config.ssh_key,worker,config.username,config.master_ip,token)
        print("Installing calico")
        subprocess.run('KUBECONFIG=./k3s.yaml kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.27.2/manifests/tigera-operator.yaml',
        shell=True,check=True, text=True)
        subprocess.run('KUBECONFIG=./k3s.yaml kubectl create -f ./calico_config.yml',
        shell=True,check=True, text=True)
        exit(0)
    elif config.cni == "canal":
        token = bootstrap_master(config.ssh_key,config.master_ip,config.username)

        for worker in config.workers:
            bootstrap_worker(config.ssh_key,worker,config.username,config.master_ip,token)
        print("Installing canal")
        subprocess.run('KUBECONFIG=./k3s.yaml kubectl create -f ./canal.yml',
        shell=True,check=True, text=True)
        exit(0)
    elif config.cni == "cilium":
        token = bootstrap_cilium_master(config.ssh_key,config.master_ip,config.username)

        for worker in config.workers:
            bootstrap_worker(config.ssh_key,worker,config.username,config.master_ip,token)
        print("Installing cilium: requires cilium-cli to be available")
        print("Warning: currently disabling kube-proxy is not supported")
        subprocess.run('KUBECONFIG=./k3s.yaml cilium install --version 1.15.4 --set=ipam.operator.clusterPoolIPv4PodCIDRList="192.168.0.0/16"',
        shell=True,check=True, text=True)
        exit(0)