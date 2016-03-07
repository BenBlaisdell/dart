from datetime import datetime
from dart.engine.redshift.admin.utils import lookup_credentials, sanitized_query


def unload_to_s3(action, conn):
    """ :type action: dart.model.action.Action """
    args = action.data.args
    aws_access_key_id, aws_secret_access_key, security_token = lookup_credentials(action)
    now = datetime.utcnow()
    sql = """
        UNLOAD ('{statement}') TO '{s3_path}'
        CREDENTIALS 'aws_access_key_id={aws_access_key_id};aws_secret_access_key={aws_secret_access_key}{token}'
        ALLOWOVERWRITE
        NULL AS 'NULL'
        ESCAPE
        DELIMITER '{delimiter}'
        PARALLEL {parallel}
        GZIP;
        """.format(
        statement=sanitized_query(args['source_sql_statement'].replace("'", "''")),
        s3_path=args['destination_s3_path'].replace(
            '{YEAR}', now.strftime('%Y')).replace(
            '{MONTH}', now.strftime('%m')).replace(
            '{DAY}', now.strftime('%d')).replace(
            '{HOUR}', now.strftime('%H')).replace(
            '{MINUTE}', now.strftime('%M')).replace(
            '{SECOND}', now.strftime('%S')),
        delimiter=args['delimiter'] if args.get('delimiter') else '\t',
        parallel='ON' if args['parallel'] else 'OFF',
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        token=';token=%s' % security_token if security_token else '',
    )
    conn.execute(sql)
