import logging
import traceback
import elasticsearch

from dart.engine.elasticsearch.admin.cluster import ElasticsearchCluster

_logger = logging.getLogger(__name__)


def force_merge_index(elasticsearch_engine, datastore, action):
    """
    :type elasitcsearch_engine: dart.engine.elasticsearch.elasticsearch.ElasticsearchEngine
    :type datastore: dart.model.datastore.Datastore
    :type action: dart.model.action.Action
    """

    cluster = ElasticsearchCluster(elasticsearch_engine, datastore)
    es = cluster.get_es_client()

    try:
        action = elasticsearch_engine.dart.patch_action(action, progress=.5)

        index = action.data.args['index']

        es.indices.forcemerge(index=index)

        elasticsearch_engine.dart.patch_action(action, progress=1)
    except Exception as e:
        error_message = e.message + '\n\n\n' + traceback.format_exc()
        raise Exception('Elasticsearch(%s) force merge index failed to execute: %s' % (elasticsearch.__version__,
                                                                                       error_message))


