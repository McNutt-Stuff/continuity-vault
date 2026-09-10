"""Hyperscaler auto-provisioning — AWS + Azure infrastructure as core plumbing.

The infrastructure-management layer of the platform: instantiate compute (VM
nodes), manage DNS, and (later) create storage. Credentials come from a
"Hyperscaler Auto-Provision" ServiceObject (kind ``hyperscaler-aws`` /
``hyperscaler-azure``) whose secrets live in a linked ConfigObject.

The headline action is ``deploy_node``: launch a VM in the chosen cloud, hand it
cloud-init user-data that downloads the control plane's bootstrap script and runs
it (so the node installs itself and heartbeats home), then publish a DNS A-record
so it has a stable, TLS-ready hostname. A background worker then waits for the
node to register — a single click takes a node from nothing to online.

All cloud SDKs are imported lazily so a missing dependency or bad credential fails
the job cleanly instead of breaking the app.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Callable

logger = logging.getLogger("cv.hyperscaler")

Progress = Callable[[str], None]


def provider_of(kind: str) -> str:
    return {"hyperscaler-aws": "aws", "hyperscaler-azure": "azure"}.get(kind or "", "")


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", (s or "").lower()).strip("-")
    return s or "node"


def build_userdata(*, role: str, name: str, fqdn: str, cp_url: str, secret: str) -> str:
    """cloud-init user-data: install the node from the control plane bundle and
    let it register itself (heartbeat). The node adopts the fleet's shared
    KEK/session/signer from the control plane automatically, so only the fleet
    node secret is injected here."""
    cp = (cp_url or "").rstrip("/")
    return (
        "#!/bin/bash\n"
        "set -e\n"
        "export DEBIAN_FRONTEND=noninteractive\n"
        f"export CV_NODE_ROLE={role}\n"
        f"export CV_NODE_NAME={name}\n"
        f"export CV_DOMAIN={fqdn}\n"
        f"export CV_CONTROL_PLANE_URL={cp}\n"
        f"export CV_NODE_SECRET={secret}\n"
        "curl -fsSL --retry 5 --retry-delay 10 "
        f"{cp}/api/nodes/bootstrap -o /tmp/arkive-node.sh\n"
        "bash /tmp/arkive-node.sh >>/var/log/arkive-bootstrap.log 2>&1\n"
    )


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #

def deploy_node(*, provider: str, config: dict, opts: dict, cp_url: str,
                fleet_secret: str, progress: Progress) -> dict:
    """Launch a VM node + publish its DNS record. Returns
    ``{instance_id, public_ip, dns_name, fqdn, region, node_name, role}``."""
    provider = (provider or "").lower()
    role = opts.get("role") or "customer-tenant"
    name = _slug(opts.get("name") or "")
    suffix = (config.get("domain_suffix") or "").strip().lstrip(".")
    fqdn = f"{name}.{suffix}" if suffix else name
    userdata = build_userdata(role=role, name=name, fqdn=fqdn, cp_url=cp_url,
                              secret=fleet_secret)
    if provider == "aws":
        vm = _aws_deploy(config, opts, name, role, userdata, progress)
    elif provider == "azure":
        vm = _azure_deploy(config, opts, name, role, userdata, progress)
    else:
        raise ValueError(f"unsupported provider '{provider}'")

    dns_name = ""
    if suffix and vm.get("public_ip"):
        try:
            if provider == "aws":
                dns_name = _aws_dns_upsert(config, fqdn, vm["public_ip"], progress)
            else:
                dns_name = _azure_dns_upsert(config, name, vm["public_ip"], progress)
        except Exception as exc:  # noqa: BLE001 — DNS failure shouldn't lose the VM
            progress(f"DNS record could not be created: {exc}")
            logger.warning("dns upsert failed: %s", exc)

    return {**vm, "dns_name": dns_name or fqdn, "fqdn": fqdn,
            "node_name": name, "role": role}


# --------------------------------------------------------------------------- #
# AWS — EC2 instance + Route53                                                 #
# --------------------------------------------------------------------------- #

def _aws_clients(config: dict, region: str):
    import boto3
    ak = (config.get("aws_access_key_id") or "").strip()
    sk = (config.get("aws_secret_access_key") or "").strip()
    if not ak or not sk:
        raise ValueError("AWS access key id + secret are required")
    session = boto3.session.Session(aws_access_key_id=ak, aws_secret_access_key=sk,
                                    region_name=region)
    return session


def _aws_latest_ubuntu(ec2) -> str:
    imgs = ec2.describe_images(
        Owners=["099720109477"],  # Canonical
        Filters=[{"Name": "name", "Values": ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]},
                 {"Name": "state", "Values": ["available"]}])["Images"]
    if not imgs:
        raise ValueError("could not resolve a base Ubuntu 22.04 AMI in this region")
    imgs.sort(key=lambda i: i.get("CreationDate", ""), reverse=True)
    return imgs[0]["ImageId"]


def _aws_deploy(config: dict, opts: dict, name: str, role: str, userdata: str,
                progress: Progress) -> dict:
    region = (opts.get("region") or config.get("region") or "us-east-1").strip()
    session = _aws_clients(config, region)
    ec2 = session.client("ec2")
    ami = (config.get("ami_id") or "").strip() or _aws_latest_ubuntu(ec2)
    itype = (opts.get("size") or config.get("instance_type") or "t3.large").strip()
    progress(f"Launching {itype} EC2 instance from {ami} in {region}…")
    run_kw: dict = {
        "ImageId": ami, "InstanceType": itype, "MinCount": 1, "MaxCount": 1,
        "UserData": userdata,
        "TagSpecifications": [{"ResourceType": "instance", "Tags": [
            {"Key": "Name", "Value": name}, {"Key": "arkive:role", "Value": role},
            {"Key": "arkive:managed", "Value": "true"}]}],
    }
    if config.get("subnet_id"):
        run_kw["SubnetId"] = config["subnet_id"].strip()
    if config.get("security_group_id"):
        run_kw["SecurityGroupIds"] = [config["security_group_id"].strip()]
    if config.get("key_name"):
        run_kw["KeyName"] = config["key_name"].strip()
    r = ec2.run_instances(**run_kw)
    iid = r["Instances"][0]["InstanceId"]
    progress(f"Instance {iid} launching — waiting for it to start…")
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    desc = ec2.describe_instances(InstanceIds=[iid])
    inst = desc["Reservations"][0]["Instances"][0]
    pub = inst.get("PublicIpAddress", "") or ""
    progress(f"Instance running at {pub or '(no public IP)'}.")
    return {"instance_id": iid, "public_ip": pub, "region": region}


def _aws_dns_upsert(config: dict, fqdn: str, ip: str, progress: Progress) -> str:
    zid = (config.get("hosted_zone_id") or "").strip()
    if not zid:
        return ""
    session = _aws_clients(config, config.get("region") or "us-east-1")
    r53 = session.client("route53")
    progress(f"Publishing Route53 record {fqdn} → {ip}…")
    r53.change_resource_record_sets(HostedZoneId=zid, ChangeBatch={"Changes": [
        {"Action": "UPSERT", "ResourceRecordSet": {
            "Name": fqdn, "Type": "A", "TTL": 300,
            "ResourceRecords": [{"Value": ip}]}}]})
    return fqdn


# --------------------------------------------------------------------------- #
# Azure — VM (public IP + NIC + VM) + Azure DNS                                #
# --------------------------------------------------------------------------- #

def _azure_cred(config: dict):
    from azure.identity import ClientSecretCredential
    for k in ("tenant_id", "client_id", "client_secret", "subscription_id"):
        if not (config.get(k) or "").strip():
            raise ValueError(f"Azure {k} is required")
    return ClientSecretCredential(config["tenant_id"].strip(),
                                  config["client_id"].strip(),
                                  config["client_secret"].strip())


def _azure_deploy(config: dict, opts: dict, name: str, role: str, userdata: str,
                  progress: Progress) -> dict:
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.network import NetworkManagementClient

    cred = _azure_cred(config)
    sub = config["subscription_id"].strip()
    rg = (config.get("resource_group") or "").strip()
    if not rg:
        raise ValueError("Azure resource_group is required")
    loc = (opts.get("region") or config.get("location") or "eastus").strip()
    vnet = (config.get("vnet") or "").strip()
    subnet = (config.get("subnet") or "").strip()
    if not vnet or not subnet:
        raise ValueError("Azure vnet + subnet are required")
    admin = (config.get("admin_username") or "arkive").strip()
    ssh_key = (config.get("ssh_public_key") or "").strip()
    admin_pw = (config.get("admin_password") or "").strip()
    if not ssh_key and not admin_pw:
        raise ValueError("Azure ssh_public_key or admin_password is required")

    net = NetworkManagementClient(cred, sub)
    comp = ComputeManagementClient(cred, sub)

    progress("Creating public IP address…")
    pip = net.public_ip_addresses.begin_create_or_update(
        rg, f"{name}-ip", {"location": loc, "sku": {"name": "Standard"},
                           "public_ip_allocation_method": "Static"}).result()
    subnet_id = (f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
                 f"Microsoft.Network/virtualNetworks/{vnet}/subnets/{subnet}")
    progress("Creating network interface…")
    nic = net.network_interfaces.begin_create_or_update(
        rg, f"{name}-nic", {"location": loc, "ip_configurations": [
            {"name": "ipcfg", "subnet": {"id": subnet_id},
             "public_ip_address": {"id": pip.id}}]}).result()

    os_profile: dict = {"computer_name": name[:15] or "arkive",
                        "admin_username": admin,
                        "custom_data": base64.b64encode(userdata.encode()).decode()}
    if ssh_key:
        os_profile["linux_configuration"] = {
            "disable_password_authentication": True,
            "ssh": {"public_keys": [{"path": f"/home/{admin}/.ssh/authorized_keys",
                                     "key_data": ssh_key}]}}
    else:
        os_profile["admin_password"] = admin_pw

    vm_size = (opts.get("size") or config.get("vm_size") or "Standard_D2s_v5").strip()
    progress(f"Creating virtual machine ({vm_size}) in {loc}…")
    vm = comp.virtual_machines.begin_create_or_update(rg, name, {
        "location": loc,
        "hardware_profile": {"vm_size": vm_size},
        "storage_profile": {"image_reference": {
            "publisher": "Canonical", "offer": "0001-com-ubuntu-server-jammy",
            "sku": "22_04-lts-gen2", "version": "latest"}},
        "os_profile": os_profile,
        "network_profile": {"network_interfaces": [{"id": nic.id}]},
    }).result()
    progress(f"Virtual machine created at {pip.ip_address or '(no public IP)'}.")
    return {"instance_id": vm.id, "public_ip": pip.ip_address or "", "region": loc}


def _azure_dns_upsert(config: dict, name_relative: str, ip: str, progress: Progress) -> str:
    from azure.mgmt.dns import DnsManagementClient
    zone = (config.get("dns_zone") or "").strip()
    rg = (config.get("dns_resource_group") or config.get("resource_group") or "").strip()
    if not zone or not rg:
        return ""
    cred = _azure_cred(config)
    dns = DnsManagementClient(cred, config["subscription_id"].strip())
    progress(f"Publishing Azure DNS record {name_relative}.{zone} → {ip}…")
    dns.record_sets.create_or_update(rg, zone, name_relative, "A",
                                     {"ttl": 300, "a_records": [{"ipv4_address": ip}]})
    return f"{name_relative}.{zone}"


# --------------------------------------------------------------------------- #
# IAM / access-policy guidance for the credentials each service object needs  #
# --------------------------------------------------------------------------- #

_AWS_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {"Sid": "ArkiveCompute", "Effect": "Allow", "Action": [
            "ec2:RunInstances", "ec2:TerminateInstances", "ec2:DescribeInstances",
            "ec2:DescribeInstanceStatus", "ec2:DescribeImages", "ec2:CreateTags",
            "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups"], "Resource": "*"},
        {"Sid": "ArkiveDns", "Effect": "Allow", "Action": [
            "route53:ChangeResourceRecordSets", "route53:ListResourceRecordSets",
            "route53:GetHostedZone", "route53:ListHostedZones"], "Resource": "*"},
    ],
}

IAM_GUIDANCE = {
    "aws": {
        "title": "AWS IAM policy — Hyperscaler Auto-Provision",
        "summary": "Create an IAM user (programmatic access) and attach this "
                   "least-privilege policy. Use its access key id + secret as the "
                   "service object credentials.",
        "credentials": ["aws_access_key_id", "aws_secret_access_key"],
        "policy": _AWS_POLICY,
        "notes": [
            "Scope ec2:RunInstances further with resource ARNs / conditions once "
            "your AMI, subnet and security group are fixed.",
            "The security group you configure must allow inbound 80/443 so the "
            "node can obtain TLS and be reachable.",
            "Route53 actions can be scoped to your specific hosted zone ARN.",
        ],
    },
    "azure": {
        "title": "Azure role assignment — Hyperscaler Auto-Provision",
        "summary": "Register an App (service principal) and grant it the roles "
                   "below. Use its Directory (tenant) ID, Application (client) ID, "
                   "a client secret, and the subscription ID as the credentials.",
        "credentials": ["tenant_id", "client_id", "client_secret", "subscription_id"],
        "roles": [
            {"role": "Contributor", "scope": "the resource group that holds the "
             "VNet/subnet and where VMs are created"},
            {"role": "DNS Zone Contributor", "scope": "your Azure DNS zone "
             "(if you publish DNS records here)"},
        ],
        "notes": [
            "Contributor on the resource group covers VM + public IP + NIC creation. "
            "Prefer a custom role scoped to Microsoft.Compute/*, "
            "Microsoft.Network/publicIPAddresses/*, Microsoft.Network/networkInterfaces/* "
            "for least privilege.",
            "The subnet you configure must permit inbound 80/443 (NSG) for TLS + reachability.",
        ],
    },
}
