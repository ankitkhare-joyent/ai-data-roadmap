"""EC2 inventory: ingest DescribeInstances snapshots, flatten them into glue.ec2.instances."""
from datetime import datetime

from airflow.providers.amazon.aws.operators.emr import EmrContainerOperator
from airflow.sdk import DAG

SPARK_CONF = {
    "spark.executor.instances": "20",
    "spark.executor.memory": "4g",
    "spark.executor.memoryOverhead": "512m",
    "spark.sql.shuffle.partitions": "2000",
    "spark.sql.adaptive.enabled": "true",
}

with DAG("ec2_inventory", schedule="10 */2 * * *", start_date=datetime(2026, 4, 1), catchup=False) as dag:
    flatten = EmrContainerOperator(
        task_id="flatten_instances",
        virtual_cluster_id="{{ var.value.emr_vc_prod_spark }}",
        execution_role_arn="{{ var.value.ec2_inventory_role }}",
        release_label="emr-7.12.0-latest",
        job_driver={"sparkSubmitJobDriver": {
            "entryPoint": "s3://acme-jobs/ec2_inventory/flatten.py",
            "sparkSubmitParameters": " ".join(f"--conf {k}={v}" for k, v in SPARK_CONF.items()),
        }},
    )
