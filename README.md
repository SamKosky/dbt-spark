## dbt-spark

### Documentation
For more information on using Spark with dbt, consult the [dbt documentation](https://docs.getdbt.com/docs/profile-spark).

### Installation
This plugin can be installed via pip:
```
# Install dbt-spark:
$ pip install dbt-spark
```

### Configuring your profile

**Connection Method**

Connections can be made to Spark in two different modes. The `http` mode is used when connecting to a managed service such as Databricks, which provides an HTTP endpoint; the `thrift` mode is used to connect directly to the master node of a cluster (either on-premise or in the cloud).

A dbt profile can be configured to run against Spark using the following configuration:

| Option  | Description                                        | Required?               | Example                  |
|---------|----------------------------------------------------|-------------------------|--------------------------|
| method    | Specify the connection method (`thrift` or `http`)   | Required   | `http`   |
| schema  | Specify the schema (database) to build models into | Required                | `analytics`              |
| host    | The hostname to connect to                         | Required                | `yourorg.sparkhost.com`  |
| port    | The port to connect to the host on                 | Optional (default: 443 for `http`, 10001 for `thrift`) | `443`                    |
| token   | The token to use for authenticating to the cluster | Required for `http`                | `abc123`                 |
| cluster | The name of the cluster to connect to              | Required for `http`               | `01234-23423-coffeetime` |
| user    | The username to use to connect to the cluster  | Optional  | `hadoop`  |
| connect_timeout | The number of seconds to wait before retrying to connect to a Pending Spark cluster | Optional (default: 10) | `60` |
| connect_retries | The number of times to try connecting to a Pending Spark cluster before giving up   | Optional (default: 0)  | `5` |

**Usage with Amazon EMR**

To connect to Spark running on an Amazon EMR cluster, you will need to run `sudo /usr/lib/spark/sbin/start-thriftserver.sh` on the master node of the cluster to start the Thrift server (see https://aws.amazon.com/premiumsupport/knowledge-center/jdbc-connection-emr/ for further context). You will also need to connect to port `10001`, which will connect to the Spark backend Thrift server; port `10000` will instead connect to a Hive backend, which will not work correctly with dbt.


**Example profiles.yml entries:**
```
your_profile_name:
  target: dev
  outputs:
    dev:
      method: http
      type: spark
      schema: analytics
      host: yourorg.sparkhost.com
      port: 443
      token: abc123
      cluster: 01234-23423-coffeetime
      connect_retries: 5
      connect_timeout: 60
```

```
your_profile_name:
  target: dev
  outputs:
    dev:
      method: thrift
      type: spark
      schema: analytics
      host: 127.0.0.1
      port: 10001
      user: hadoop
      connect_retries: 5
      connect_timeout: 60
```



### Usage Notes

**Model Configuration**

The following configurations can be supplied to models run with the dbt-spark plugin:


| Option      | Description                                        | Required?               | Example                |
|-------------|----------------------------------------------------|-------------------------|------------------------|
| file_format | The file format to use when creating tables (`parquet`, `delta`, `csv`, `json`, `text`, `jdbc`, `orc`, `hive` or `libsvm`). | Optional | `delta` |
| incremental_strategy | The strategy to use for incremental models (`insert_overwrite` or `merge`). Note `merge` requires `file_format` = `delta` and `unique_key` to be specified. | Optional (default: `insert_overwrite`) | `merge` |
| partition_by | Partition the created table by the specified columns. A directory is created for each partition. | Required | `['date_day']` |
| cluster_by | Each partition in the created table will be split into a fixed number of buckets by the specified columns. This is typically used with partitioning to read and shuffle less data. | Optional                 | `['name', 'address']`              |
| num_buckets | Used in conjunction with `cluster_by`. | Optional (required if `cluster_by` is specified) | `3` |



**Incremental Models**

To use incremental models, specify a `partition_by` clause in your model config. The default incremental strategy used is `insert_overwrite`, which will overwrite the partitions included in your query. Be sure to re-select _all_ of the relevant
data for a partition when using the `insert_overwrite` strategy.

```
{{ config(
    materialized='incremental',
    partition_by=['date_day'],
    file_format='parquet'
) }}

/*
  Every partition returned by this query will be overwritten
  when this model runs
*/

select
    date_day,
    count(*) as users

from {{ ref('events') }}
where date_day::date >= '2019-01-01'
group by 1
```

The `merge` strategy is only supported when using file_format `delta` (supported in Databricks). It also requires you to specify a `unique key` to match existing records.

```
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    partition_by=['date_day'],
    file_format='delta'
) }}

select *
from {{ ref('events') }}
{% if is_incremental() %}
  where date_day > (select max(date_day) from {{ this }})
{% endif %}
```

### Reporting bugs and contributing code

-   Want to report a bug or request a feature? Let us know on [Slack](http://slack.getdbt.com/), or open [an issue](https://github.com/fishtown-analytics/dbt-spark/issues/new).

## Code of Conduct

Everyone interacting in the dbt project's codebases, issue trackers, chat rooms, and mailing lists is expected to follow the [PyPA Code of Conduct](https://www.pypa.io/en/latest/code-of-conduct/).
