import copy
import json
import ntpath
import os
import math
import time

from dart.engine.emr.actions.consume_subscription import subscription_s3_path_and_file_size_generator
from dart.engine.emr.actions.load_dataset import load_dataset_s3_path_and_file_size_generator
from dart.engine.emr.metadata import EmrActionTypes
from dart.engine.emr.mappings import instance_disk_space_map
from dart.model.action import ActionState
from dart.model.query import Filter, Operator
from dart.util.s3 import s3_copy_recursive
from dart.util.shell import call


def start_datastore(emr_engine, datastore, action):
    """
    :type emr_engine: dart.engine.emr.emr.EmrEngine
    :type datastore: dart.model.datastore.Datastore
    :type action: dart.model.action.Action
    """
    cluster_name = 'dart-datastore-%s-%s' % (datastore.id, datastore.data.name)
    actions = emr_engine.dart.find_actions([
        Filter('datastore_id', Operator.EQ, datastore.id),
        Filter('state', Operator.EQ, ActionState.HAS_NEVER_RUN),
        Filter('action_type_name', Operator.EQ, EmrActionTypes.load_dataset.name),
    ])
    instance_groups_args = prepare_instance_groups(emr_engine, datastore, actions, emr_engine.core_node_limit)
    bootstrap_actions_args = prepare_bootstrap_actions(datastore, emr_engine.impala_docker_repo_base_url,
                                                       emr_engine.impala_version)

    extra_data = {
        'instance_groups_args': instance_groups_args,
        'bootstrap_action_args': bootstrap_actions_args,
    }
    if datastore.data.args['dry_run']:
        emr_engine.dart.patch_action(action, progress=1, extra_data=extra_data)
        return

    action = emr_engine.dart.patch_action(action, progress=0, extra_data=extra_data)

    cluster_id = create_cluster(bootstrap_actions_args, cluster_name, datastore, emr_engine, instance_groups_args)
    emr_engine.dart.patch_datastore(datastore, extra_data={'cluster_id': cluster_id})
    emr_engine.dart.patch_action(action, progress=0.1)

    cluster = None
    while True:
        cluster = emr_engine.conn.describe_jobflow(cluster_id)
        # http://docs.aws.amazon.com/ElasticMapReduce/latest/DeveloperGuide/ProcessingCycle.html
        if cluster.state in ['STARTING', 'BOOTSTRAPPING', 'RUNNING']:
            time.sleep(30)
        else:
            break

    if cluster.state not in ['WAITING']:
        raise Exception('cluster_id=%s not in WAITING state, but in state: %s' % (cluster_id, cluster.state))

    emr_engine.dart.patch_datastore(
        datastore,
        host=cluster.masterpublicdnsname,
        port=21050,
        connection_url='jdbc:impala://%s:%s/default' % (cluster.masterpublicdnsname, 21050)
    )


def create_cluster(bootstrap_actions_args, cluster_name, datastore, emr_engine, instance_groups_args):
    keyname = emr_engine.ec2_keyname
    instance_profile = emr_engine.instance_profile
    az = emr_engine.cluster_availability_zone
    cmd = 'aws emr create-cluster' \
          ' --release-label {release_label}'\
          ' --instance-type {instance_type}'\
          ' --instance-count {instance_count}'\
          ' --name {cluster_name}'\
          ' --log-uri {log_uri}'\
          ' --service-role {service_role}'\
          ' --configurations {configurations}'\
          ' --ec2-attributes {ec2_attributes}'\
          ' --enable-debugging'\
          ' --tags {tags}'\
          ' --bootstrap-actions {bootstrap_actions}'\
          ' --applications {applications}'\
          ''
    cmd = cmd.format(
        release_label=datastore.data.args['release_label'],
        instance_type=datastore.data.args['instance_type'],
        instance_count=instance_groups_args[1][0] + 1,
        cluster_name=cluster_name,
        log_uri=datastore.data.s3_logs_path,
        service_role=emr_engine.service_role,
        configurations='file://%s/start_configs.json' % os.path.dirname(os.path.abspath(__file__)),
        ec2_attributes='KeyName=%s,AvailabilityZone=%s,InstanceProfile=%s' % (keyname, az, instance_profile),
        tags=' '.join(['%s=%s' % (k, v) for k, v in emr_engine.cluster_tags.iteritems()]),
        bootstrap_actions=' '.join([
            'Path="{path}",Name="{name}",Args=[{args}]'.format(
                name=a[0],
                path=a[1],
                args='' if len(a[2:]) <= 0 else '"%s"' % (','.join(a[2:]))) for a in bootstrap_actions_args
        ]),
        applications='Name=Hadoop Name=Hive Name=Spark',
    )
    result = call(cmd)
    return json.loads(result)['ClusterId']


def prepare_instance_groups(emr_engine, datastore, actions, core_node_limit):
    instance_type = datastore.data.args.get('instance_type', 'm3.2xlarge')
    legacy_core_instance_count = datastore.data.args.get('core_instance_count')
    legacy_core_instance_count = legacy_core_instance_count + 1 if legacy_core_instance_count else None
    instance_count = datastore.data.args.get('instance_count', legacy_core_instance_count)
    if instance_count:
        return [
            (1, 'MASTER', instance_type, 'ON_DEMAND', 'master'),
            (max(instance_count - 1, 1), 'CORE', instance_type, 'ON_DEMAND', 'core'),
        ]

    s3_path_and_file_size_generators_by_action_name = {
        EmrActionTypes.load_dataset.name: load_dataset_s3_path_and_file_size_generator,
        EmrActionTypes.consume_subscription.name: subscription_s3_path_and_file_size_generator,
    }
    dataset_size = 0L
    for action in actions:
        generator = s3_path_and_file_size_generators_by_action_name[action.data.action_type_name]
        dataset_size += sum(file_size for s3_path, file_size in generator(emr_engine, action))

    data_to_freespace_ratio = float(datastore.data.args['data_to_freespace_ratio'])
    num_cores = dataset_size / float((instance_disk_space_map[instance_type] * data_to_freespace_ratio))
    num_cores = int(math.ceil(num_cores))
    num_cores = max(1, num_cores)
    if num_cores > core_node_limit:
        raise Exception('too many nodes required!')

    return [
        (1, 'MASTER', instance_type, 'ON_DEMAND', 'master'),
        (num_cores, 'CORE', instance_type, 'ON_DEMAND', 'core'),
    ]


def prepare_bootstrap_actions(datastore, impala_docker_repo_base_url, impala_version, dry_run=False):
    current_path, current_file = ntpath.split(os.path.abspath(__file__))
    s3_ba_path = '%s/%s/bootstrap_actions' % (datastore.data.s3_artifacts_path, datastore.data.engine_name)
    local_ba_path = '%s/../bootstrap_actions/' % current_path
    if not dry_run:
        s3_copy_recursive(local_ba_path, s3_ba_path)

    wrapper_script_path = s3_ba_path + '/shell/install_impala_wrapper.sh'
    install_impala_path = s3_ba_path + '/ruby/install_impala.rb'
    copy_resources_path = s3_ba_path + '/shell/copy_resources.sh'
    csv_serde_path = s3_ba_path + '/misc/csv-serde-1.1.2-0.11.0-all.jar'
    return [
        ('dart: install impala (background)', wrapper_script_path, install_impala_path, impala_docker_repo_base_url,
         impala_version),
        ('dart: copy resources', copy_resources_path, csv_serde_path),
    ]
