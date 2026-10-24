"""
MonitoringStack: centralised CloudWatch alarms, a single alerting SNS topic,
and an operations dashboard for the RecipeMate platform (OR-6).

Why a central stack rather than inline-per-stack alarms:
  - It matches the project's one-concern-per-stack style and the way
    PipelineStack consumes other stacks' constructs via keyword-only args in
    ``infra/app.py`` (which both passes the references AND establishes the
    CloudFormation dependency ordering so the observed stacks build first).
  - The operational-readiness requirement (OR-6) calls for ONE alerting topic
    distinct from the APNs notifications topic AND ONE dashboard summarising
    every tier. Both are naturally a single central construct rather than
    scattered fragments across seven stacks.

What it creates:
  - A dedicated alerting ``sns.Topic`` (email and/or Slack-webhook
    subscription, both context-driven). This topic is explicitly DISTINCT
    from the APNs "notifications" topic: that one is NOT created in infra at
    all. It is referenced only by ARN via the ``snsNotificationsTopicArn``
    context key and surfaced to the DLQ Lambda as the
    ``SNS_NOTIFICATIONS_TOPIC_ARN`` env var (see ``queue_stack.py`` and
    ``docs/configuration.md``). Never reuse the notifications topic here.
  - Alarms across every tier, all wired to the alerting topic via
    ``cwa.SnsAction`` for both the ALARM and OK transitions:
      * ECS    – service CPU high, memory high, running task count below
                 desired.
      * ALB    – load-balancer + target 5xx count, unhealthy host count, p95
                 target response time.
      * API GW – server (5xx) errors, p99 latency, and a documented
                 client-error (4xx incl. 429) proxy for the throttle-count
                 requirement (HTTP API v2 publishes no per-API Throttle
                 metric; that is a REST API v1 metric).
      * RDS    – CPU, free storage, connection count, and a prod/multi-AZ
                 only tightened CPU alarm standing in for the
                 replica/failover requirement (see the RDS section for why no
                 clean standard failover metric exists at this pin).
      * SQS    – DLQ depth > 0 and main-queue age of oldest message.
      * Lambda – extraction error count, throttles, and p99 duration near the
                 120s timeout.
      * Redis  – evictions, memory usage percentage, and engine CPU (raw
                 AWS/ElastiCache metrics because the cluster is an L1 Cfn
                 construct with no metric helpers).
  - A ``cw.Dashboard`` summarising every tier (one graph row per tier plus an
    alarm-status row), so operators have a single pane of glass.

Per-env gating:
  - Thresholds are intentionally conservative initial values to be refined
    after launch once real baselines exist.
  - The replica/failover-class RDS alarm is created ONLY for prod or when the
    ``multiAz`` context key is truthy (single-AZ staging has no standby to
    fail over to).

Properties exposed for other stacks / operators:
  - ``alerting_topic`` – the dedicated alerting ``sns.Topic``.

Cross-stack references (keyword-only), supplied by ``infra/app.py`` AFTER the
observed stacks are instantiated so the references resolve and the dependency
edges are correct:
  - ``fargate_service``  – ApiStack's ``ecs.FargateService``.
  - ``alb``              – ApiStack's ``elbv2.ApplicationLoadBalancer``.
  - ``target_group``     – ApiStack's ``elbv2.ApplicationTargetGroup``.
  - ``http_api``         – ApiStack's ``apigwv2.HttpApi``.
  - ``db_instance``      – DatabaseStack's ``rds.DatabaseInstance``.
  - ``extraction_queue`` – QueueStack's main ``sqs.Queue``.
  - ``extraction_dlq``   – QueueStack's dead-letter ``sqs.Queue``.
  - ``extraction_lambda``– QueueStack's extraction ``lambda.Function``.
  - ``dlq_lambda``       – QueueStack's DLQ-consumer ``lambda.Function``.
  - ``redis_cluster``    – CacheStack's ``elasticache.CfnCacheCluster``.
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cwa,
    aws_ecs as ecs,
    aws_elasticache as elasticache,
    aws_elasticloadbalancingv2 as elbv2,
    aws_lambda as aws_lambda,
    aws_rds as rds,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_sqs as sqs,
)
from constructs import Construct

# The extraction Lambda's timeout is LAMBDA_TIMEOUT = Duration.seconds(120) in
# queue_stack.py. We alarm when p99 duration approaches that ceiling so a slow
# trend is caught before invocations start timing out. 120s = 120000 ms; we
# fire at roughly 80% of it.
LAMBDA_TIMEOUT_MS = 120_000
LAMBDA_DURATION_ALARM_MS = int(LAMBDA_TIMEOUT_MS * 0.8)  # 96000 ms

# RDS free-storage floor. The staging instance has 20 GiB of GP2 storage; we
# alarm when free space falls below ~2 GiB. Expressed in bytes because the
# AWS/RDS FreeStorageSpace metric reports bytes.
RDS_FREE_STORAGE_FLOOR_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


class MonitoringStack(Stack):
    """Central CloudWatch alarms, alerting SNS topic, and dashboard (OR-6).

    See the module docstring for the full rationale, the list of alarms per
    tier, and the per-env gating rules.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        env_name: str = "staging",
        fargate_service: ecs.FargateService,
        alb: elbv2.ApplicationLoadBalancer,
        target_group: elbv2.ApplicationTargetGroup,
        http_api: apigwv2.HttpApi,
        db_instance: rds.DatabaseInstance,
        extraction_queue: sqs.Queue,
        extraction_dlq: sqs.Queue,
        extraction_lambda: aws_lambda.Function,
        dlq_lambda: aws_lambda.Function,
        redis_cluster: elasticache.CfnCacheCluster,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        self._env_name = env_name
        # Alarms are collected here so the dashboard's alarm-status row can
        # reference every one of them without re-listing them by hand.
        self._alarms: list[cw.Alarm] = []

        # ------------------------------------------------------------------ #
        # Dedicated alerting SNS topic.                                       #
        #                                                                    #
        # This is a BRAND-NEW topic, explicitly DISTINCT from the APNs        #
        # "notifications" topic. The notifications topic is never created in  #
        # infra: it is referenced only by ARN via the                        #
        # ``snsNotificationsTopicArn`` context key and handed to the DLQ      #
        # Lambda as SNS_NOTIFICATIONS_TOPIC_ARN. Operator alerting and        #
        # end-user push are different concerns, so they get different topics. #
        # ------------------------------------------------------------------ #
        self._alerting_topic = sns.Topic(
            self,
            "AlertingTopic",
            display_name=f"RecipeMate {env_name} Alerts",
            topic_name=f"{self.stack_name}-alerts",
        )

        # ------------------------------------------------------------------ #
        # Subscription target — context-driven with documented fallbacks.    #
        #                                                                    #
        # Supply via ``-c alertEmail=ops@example.com`` and/or                 #
        # ``-c alertSlackWebhookUrl=https://hooks.slack.com/...``. When       #
        # NEITHER is set we add no subscription (and emit a note) rather than #
        # raising, so credential-free synth never crashes and no real email   #
        # is ever hardcoded. An email subscription must be confirmed via the  #
        # link AWS emails before alarms deliver.                              #
        # ------------------------------------------------------------------ #
        alert_email = self.node.try_get_context("alertEmail")
        alert_slack = self.node.try_get_context("alertSlackWebhookUrl")

        if alert_email:
            self._alerting_topic.add_subscription(
                subs.EmailSubscription(alert_email)
            )
        if alert_slack:
            # Slack incoming webhooks are HTTPS POST endpoints; SNS delivers the
            # alarm payload to them directly.
            self._alerting_topic.add_subscription(
                subs.UrlSubscription(
                    alert_slack, protocol=sns.SubscriptionProtocol.HTTPS
                )
            )
        if not alert_email and not alert_slack:
            # No endpoint configured: the topic still exists (so alarms wire to
            # something), but nothing is subscribed yet. Surface a note so the
            # operator knows to add an endpoint before relying on paging.
            cdk.CfnOutput(
                self,
                "AlertingSubscriptionNote",
                value=(
                    "No alertEmail or alertSlackWebhookUrl context set; "
                    "subscribe an endpoint to the alerting topic before launch"
                ),
                description="OR-6 alerting topic has no subscription yet",
            )

        # ------------------------------------------------------------------ #
        # Build the per-tier alarms.                                          #
        # ------------------------------------------------------------------ #
        self._add_ecs_alarms(fargate_service)
        self._add_alb_alarms(alb, target_group)
        self._add_api_gateway_alarms(http_api)
        self._add_rds_alarms(db_instance)
        self._add_sqs_alarms(extraction_queue, extraction_dlq)
        self._add_lambda_alarms(extraction_lambda)
        self._add_redis_alarms(redis_cluster)

        # ------------------------------------------------------------------ #
        # Dashboard — one pane of glass across every tier.                   #
        # ------------------------------------------------------------------ #
        self._build_dashboard(
            fargate_service=fargate_service,
            alb=alb,
            target_group=target_group,
            http_api=http_api,
            db_instance=db_instance,
            extraction_queue=extraction_queue,
            extraction_dlq=extraction_dlq,
            extraction_lambda=extraction_lambda,
            redis_cluster=redis_cluster,
        )

        # ------------------------------------------------------------------ #
        # Outputs so operators can find the topic and dashboard quickly.     #
        # ------------------------------------------------------------------ #
        cdk.CfnOutput(
            self,
            "AlertingTopicArn",
            value=self._alerting_topic.topic_arn,
            description="ARN of the dedicated OR-6 alerting SNS topic",
        )
        cdk.CfnOutput(
            self,
            "DashboardName",
            value=self._dashboard.dashboard_name,
            description="Name of the OR-6 operations dashboard",
        )

    # ---------------------------------------------------------------------- #
    # Alarm helper                                                            #
    # ---------------------------------------------------------------------- #

    def _alarm(
        self,
        id: str,
        *,
        metric: cw.IMetric,
        threshold: float,
        comparison: cw.ComparisonOperator,
        evaluation_periods: int,
        description: str,
        datapoints_to_alarm: int | None = None,
        treat_missing_data: cw.TreatMissingData = (
            cw.TreatMissingData.NOT_BREACHING
        ),
    ) -> cw.Alarm:
        """Create a ``cw.Alarm`` wired to the alerting topic and track it.

        Every alarm fires an ``SnsAction`` to the alerting topic on BOTH the
        ALARM and OK transitions so operators see recovery as well as onset.
        The created alarm is appended to ``self._alarms`` so the dashboard's
        alarm-status row picks it up automatically.
        """
        alarm = cw.Alarm(
            self,
            id,
            metric=metric,
            threshold=threshold,
            comparison_operator=comparison,
            evaluation_periods=evaluation_periods,
            datapoints_to_alarm=datapoints_to_alarm,
            alarm_description=description,
            treat_missing_data=treat_missing_data,
        )
        action = cwa.SnsAction(self._alerting_topic)
        alarm.add_alarm_action(action)
        alarm.add_ok_action(action)
        self._alarms.append(alarm)
        return alarm

    # ---------------------------------------------------------------------- #
    # ECS alarms                                                              #
    # ---------------------------------------------------------------------- #

    def _add_ecs_alarms(self, service: ecs.FargateService) -> None:
        """Service CPU/memory high and running-task-count-below-desired."""
        self._alarm(
            "EcsCpuHigh",
            metric=service.metric_cpu_utilization(period=Duration.minutes(5)),
            threshold=85,
            comparison=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=3,
            datapoints_to_alarm=3,
            description=(
                "ECS service CPU utilisation >= 85% for 15 minutes. Initial "
                "threshold to refine post-launch."
            ),
        )
        self._alarm(
            "EcsMemoryHigh",
            metric=service.metric_memory_utilization(
                period=Duration.minutes(5)
            ),
            threshold=85,
            comparison=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=3,
            datapoints_to_alarm=3,
            description=(
                "ECS service memory utilisation >= 85% for 15 minutes. Initial "
                "threshold to refine post-launch."
            ),
        )

        # Running task count below desired. The FargateService exposes no
        # helper for RunningTaskCount, so we read it from Container Insights
        # (already enabled on the cluster via container_insights=True in
        # ApiStack). Desired count is context-driven (app.py publishes
        # apiDesiredCount) with a documented fallback of 1.
        desired_count = self.node.try_get_context("apiDesiredCount") or 1
        running_task_count = cw.Metric(
            namespace="ECS/ContainerInsights",
            metric_name="RunningTaskCount",
            dimensions_map={
                "ClusterName": service.cluster.cluster_name,
                "ServiceName": service.service_name,
            },
            statistic="Minimum",
            period=Duration.minutes(5),
        )
        self._alarm(
            "EcsRunningTasksBelowDesired",
            metric=running_task_count,
            threshold=float(desired_count),
            comparison=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            evaluation_periods=3,
            datapoints_to_alarm=2,
            description=(
                f"ECS running task count below the desired {desired_count}. "
                "Indicates tasks are crashing or failing health checks. "
                "Treat missing data as breaching so a fully-down service "
                "(no datapoints) still pages."
            ),
            treat_missing_data=cw.TreatMissingData.BREACHING,
        )

    # ---------------------------------------------------------------------- #
    # ALB alarms                                                              #
    # ---------------------------------------------------------------------- #

    def _add_alb_alarms(
        self,
        alb: elbv2.ApplicationLoadBalancer,
        target_group: elbv2.ApplicationTargetGroup,
    ) -> None:
        """5xx rate, unhealthy host count, and p95 target response time."""
        # Sum of load-balancer-generated 5xx (ELB_5XX) and target-generated
        # 5xx (TARGET_5XX) so both backend errors and ALB-level failures
        # (e.g. no healthy targets) are covered by one alarm. The ``.metrics``
        # facade is the non-deprecated accessor at this pin (the bare
        # ``metric_http_code_*`` helpers are deprecated but still functional).
        elb_5xx = alb.metrics.http_code_elb(
            elbv2.HttpCodeElb.ELB_5XX_COUNT,
            period=Duration.minutes(5),
            statistic="Sum",
        )
        target_5xx = target_group.metrics.http_code_target(
            elbv2.HttpCodeTarget.TARGET_5XX_COUNT,
            period=Duration.minutes(5),
            statistic="Sum",
        )
        total_5xx = cw.MathExpression(
            expression="elb5xx + target5xx",
            using_metrics={"elb5xx": elb_5xx, "target5xx": target_5xx},
            label="ALB total 5xx count",
            period=Duration.minutes(5),
        )
        self._alarm(
            "Alb5xxHigh",
            metric=total_5xx,
            threshold=10,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            description=(
                "More than 10 ALB 5xx responses (load balancer + target) in 5 "
                "minutes. Initial threshold to refine post-launch."
            ),
        )
        self._alarm(
            "AlbUnhealthyHosts",
            metric=target_group.metrics.unhealthy_host_count(
                statistic="Maximum", period=Duration.minutes(1)
            ),
            threshold=1,
            comparison=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=3,
            datapoints_to_alarm=2,
            description=(
                "At least one ALB target reporting unhealthy. Sustained means "
                "the Fargate /health check is failing."
            ),
        )
        self._alarm(
            "AlbTargetResponseTimeP95",
            metric=target_group.metrics.target_response_time(
                statistic="p95", period=Duration.minutes(5)
            ),
            # target_response_time reports SECONDS.
            threshold=2,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=3,
            datapoints_to_alarm=3,
            description=(
                "ALB p95 target response time above 2 seconds for 15 minutes. "
                "Initial threshold to refine post-launch."
            ),
        )

    # ---------------------------------------------------------------------- #
    # API Gateway alarms                                                      #
    # ---------------------------------------------------------------------- #

    def _add_api_gateway_alarms(self, http_api: apigwv2.HttpApi) -> None:
        """5xx, p99 latency, and a documented throttle proxy.

        NOTE on the throttle requirement: API Gateway v2 HTTP APIs do NOT
        publish a per-API Throttle metric (that is a REST API v1 / AWS/ApiGateway
        metric). We approximate throttle/over-limit visibility with the 4xx
        ClientError metric, which includes the 429 responses emitted once OR-5
        route throttling is configured. This is a documented proxy for the
        "throttle count" line item, not a true throttle metric.
        """
        self._alarm(
            "ApiGateway5xxHigh",
            metric=http_api.metric_server_error(period=Duration.minutes(5)),
            threshold=5,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            description=(
                "More than 5 API Gateway 5xx responses in 5 minutes. Initial "
                "threshold to refine post-launch."
            ),
        )
        self._alarm(
            "ApiGatewayLatencyP99",
            metric=http_api.metric_latency(
                period=Duration.minutes(5), statistic="p99"
            ),
            # API Gateway Latency is reported in MILLISECONDS.
            threshold=3000,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=3,
            datapoints_to_alarm=3,
            description=(
                "API Gateway p99 latency above 3000 ms for 15 minutes. Initial "
                "threshold to refine post-launch."
            ),
        )
        self._alarm(
            "ApiGatewayClientErrorProxy",
            metric=http_api.metric_client_error(period=Duration.minutes(5)),
            threshold=50,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            description=(
                "Proxy for the throttle-count requirement: elevated 4xx "
                "(ClientError) on the HTTP API, which includes 429 once OR-5 "
                "route throttling is live. HTTP API v2 publishes no per-API "
                "Throttle metric, so this ClientError signal stands in for it."
            ),
        )

    # ---------------------------------------------------------------------- #
    # RDS alarms                                                              #
    # ---------------------------------------------------------------------- #

    def _add_rds_alarms(self, db_instance: rds.DatabaseInstance) -> None:
        """CPU, free storage, connection count, and a prod/multi-AZ alarm.

        On the replica/failover line item: at the pinned aws-cdk-lib 2.144.0
        there is no clean standard single-instance "failover" metric for RDS
        (ReplicaLag is an Aurora/read-replica concept, not Multi-AZ standby
        failover, and there is no public FailoverTime metric). Rather than
        reference a non-existent metric, when running prod or with the
        ``multiAz`` context key we add a TIGHTER CPU alarm as the closest
        standard failover-health signal and document the choice here.
        """
        self._alarm(
            "RdsCpuHigh",
            metric=db_instance.metric_cpu_utilization(
                period=Duration.minutes(5)
            ),
            threshold=80,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=3,
            datapoints_to_alarm=3,
            description=(
                "RDS CPU utilisation above 80% for 15 minutes. Initial "
                "threshold to refine post-launch."
            ),
        )
        self._alarm(
            "RdsFreeStorageLow",
            metric=db_instance.metric_free_storage_space(
                period=Duration.minutes(5)
            ),
            # FreeStorageSpace is reported in BYTES; floor is ~2 GiB.
            threshold=RDS_FREE_STORAGE_FLOOR_BYTES,
            comparison=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            evaluation_periods=1,
            description=(
                "RDS free storage below ~2 GiB "
                f"({RDS_FREE_STORAGE_FLOOR_BYTES} bytes). Risk of the instance "
                "running out of disk."
            ),
        )
        self._alarm(
            "RdsConnectionsHigh",
            metric=db_instance.metric_database_connections(
                period=Duration.minutes(5)
            ),
            # db.t4g.micro supports well over this; 80 is a conservative
            # initial ceiling that still leaves headroom for a connection leak.
            threshold=80,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=3,
            datapoints_to_alarm=3,
            description=(
                "RDS open connection count above 80 for 15 minutes. Initial "
                "threshold to refine post-launch; may indicate a connection "
                "leak or pool misconfiguration."
            ),
        )

        # Prod / Multi-AZ only: gate on env_name == 'prod' OR the multiAz
        # context key (the same key DatabaseStack reads to enable Multi-AZ).
        is_multi_az = bool(self.node.try_get_context("multiAz"))
        if self._env_name == "prod" or is_multi_az:
            self._alarm(
                "RdsCpuHighProd",
                metric=db_instance.metric_cpu_utilization(
                    period=Duration.minutes(5)
                ),
                # Tighter than the baseline 80% alarm: on a Multi-AZ instance a
                # sustained CPU climb can precede or follow a standby failover,
                # so prod gets an earlier warning. Documented stand-in for the
                # replica/failover line item (no clean standard failover metric
                # exists at this pin).
                threshold=70,
                comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=3,
                datapoints_to_alarm=3,
                description=(
                    "PROD/Multi-AZ only: RDS CPU above 70% for 15 minutes. "
                    "Tighter early-warning standing in for the "
                    "replica/failover requirement; no standard Multi-AZ "
                    "failover metric is available at aws-cdk-lib 2.144.0."
                ),
            )

    # ---------------------------------------------------------------------- #
    # SQS alarms                                                              #
    # ---------------------------------------------------------------------- #

    def _add_sqs_alarms(
        self, queue: sqs.Queue, dlq: sqs.Queue
    ) -> None:
        """DLQ depth > 0 and main-queue age of oldest message."""
        self._alarm(
            "SqsDlqNotEmpty",
            metric=dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(1), statistic="Maximum"
            ),
            threshold=0,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            description=(
                "At least one message in the extraction dead-letter queue: a "
                "job exhausted its retries and permanently failed."
            ),
        )
        self._alarm(
            "SqsOldestMessageAge",
            metric=queue.metric_approximate_age_of_oldest_message(
                period=Duration.minutes(5), statistic="Maximum"
            ),
            # Age is in SECONDS. The Lambda timeout is 120s and the queue
            # visibility timeout is 300s, so a sustained backlog older than
            # 600s means consumption is not keeping up.
            threshold=600,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=2,
            description=(
                "Oldest extraction message older than 600 seconds: the "
                "consumer is falling behind (Lambda timeout 120s, visibility "
                "timeout 300s). Initial threshold to refine post-launch."
            ),
        )

    # ---------------------------------------------------------------------- #
    # Lambda alarms                                                           #
    # ---------------------------------------------------------------------- #

    def _add_lambda_alarms(
        self, extraction_lambda: aws_lambda.Function
    ) -> None:
        """Extraction error count, throttles, and p99 duration near timeout."""
        self._alarm(
            "LambdaErrors",
            metric=extraction_lambda.metric_errors(
                period=Duration.minutes(5), statistic="Sum"
            ),
            threshold=1,
            comparison=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            description=(
                "At least one extraction Lambda error in 5 minutes. A failed "
                "invocation is retried and eventually dead-letters, so even a "
                "single error is worth surfacing early."
            ),
        )
        self._alarm(
            "LambdaThrottles",
            metric=extraction_lambda.metric_throttles(
                period=Duration.minutes(5), statistic="Sum"
            ),
            threshold=1,
            comparison=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            description=(
                "Extraction Lambda is being throttled: concurrency limit hit, "
                "so jobs are delayed."
            ),
        )
        self._alarm(
            "LambdaDurationNearTimeout",
            metric=extraction_lambda.metric_duration(
                period=Duration.minutes(5), statistic="p99"
            ),
            # Duration is in MILLISECONDS. Fire at ~80% of the 120s
            # (120000 ms) LAMBDA_TIMEOUT so a slow trend is caught before
            # invocations start timing out.
            threshold=LAMBDA_DURATION_ALARM_MS,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=3,
            datapoints_to_alarm=3,
            description=(
                "Extraction Lambda p99 duration above "
                f"{LAMBDA_DURATION_ALARM_MS} ms (~80% of the 120s/"
                f"{LAMBDA_TIMEOUT_MS} ms timeout): invocations are at risk of "
                "timing out."
            ),
        )

    # ---------------------------------------------------------------------- #
    # Redis alarms                                                            #
    # ---------------------------------------------------------------------- #

    def _redis_metric(
        self,
        redis_cluster: elasticache.CfnCacheCluster,
        metric_name: str,
        statistic: str,
    ) -> cw.Metric:
        """Build a raw AWS/ElastiCache metric for the single-node cluster.

        CacheStack's ``redis`` is an L1 ``CfnCacheCluster`` with no metric
        helper methods, so we construct raw ``cw.Metric`` objects dimensioned
        on the cluster ref (its CacheClusterId).
        """
        return cw.Metric(
            namespace="AWS/ElastiCache",
            metric_name=metric_name,
            dimensions_map={"CacheClusterId": redis_cluster.ref},
            statistic=statistic,
            period=Duration.minutes(5),
        )

    def _add_redis_alarms(
        self, redis_cluster: elasticache.CfnCacheCluster
    ) -> None:
        """Evictions, memory usage percentage, and engine CPU.

        All three are standard AWS/ElastiCache Redis metrics:
          - ``Evictions`` (Sum): keys evicted due to the maxmemory limit.
          - ``DatabaseMemoryUsagePercentage`` (Average): percent of the
            node's memory in use.
          - ``EngineCPUUtilization`` (Average): CPU of the Redis engine
            process, the Redis-appropriate signal (preferred over the
            host-level ``CPUUtilization``).
        """
        self._alarm(
            "RedisEvictions",
            metric=self._redis_metric(redis_cluster, "Evictions", "Sum"),
            threshold=100,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            description=(
                "More than 100 Redis evictions in 5 minutes: the cache is "
                "under memory pressure and dropping keys. Initial threshold to "
                "refine post-launch."
            ),
        )
        self._alarm(
            "RedisMemoryHigh",
            metric=self._redis_metric(
                redis_cluster, "DatabaseMemoryUsagePercentage", "Average"
            ),
            threshold=80,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=3,
            datapoints_to_alarm=3,
            description=(
                "Redis memory usage above 80% for 15 minutes: approaching the "
                "maxmemory limit where evictions begin."
            ),
        )
        self._alarm(
            "RedisCpuHigh",
            metric=self._redis_metric(
                redis_cluster, "EngineCPUUtilization", "Average"
            ),
            threshold=75,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=3,
            datapoints_to_alarm=3,
            description=(
                "Redis engine CPU above 75% for 15 minutes. Initial threshold "
                "to refine post-launch."
            ),
        )

    # ---------------------------------------------------------------------- #
    # Dashboard                                                               #
    # ---------------------------------------------------------------------- #

    def _build_dashboard(
        self,
        *,
        fargate_service: ecs.FargateService,
        alb: elbv2.ApplicationLoadBalancer,
        target_group: elbv2.ApplicationTargetGroup,
        http_api: apigwv2.HttpApi,
        db_instance: rds.DatabaseInstance,
        extraction_queue: sqs.Queue,
        extraction_dlq: sqs.Queue,
        extraction_lambda: aws_lambda.Function,
        redis_cluster: elasticache.CfnCacheCluster,
    ) -> None:
        """Assemble one graph row per tier plus an alarm-status row."""
        self._dashboard = cw.Dashboard(
            self,
            "Dashboard",
            dashboard_name=f"{self.stack_name}-overview",
        )

        self._dashboard.add_widgets(
            cw.TextWidget(
                markdown=(
                    f"# RecipeMate {self._env_name} operations\n"
                    "CloudWatch alarms (OR-6) across ECS, ALB, API Gateway, "
                    "RDS, SQS, Lambda, and Redis. All alarms page the alerting "
                    "SNS topic. Thresholds are initial values to refine "
                    "post-launch."
                ),
                width=24,
                height=3,
            )
        )

        self._dashboard.add_widgets(
            cw.GraphWidget(
                title="ECS — CPU / memory utilisation (%)",
                left=[
                    fargate_service.metric_cpu_utilization(
                        period=Duration.minutes(5)
                    ),
                    fargate_service.metric_memory_utilization(
                        period=Duration.minutes(5)
                    ),
                ],
                width=12,
                height=6,
            ),
            cw.GraphWidget(
                title="ALB — 5xx / unhealthy hosts / p95 response time",
                left=[
                    alb.metrics.http_code_elb(
                        elbv2.HttpCodeElb.ELB_5XX_COUNT,
                        period=Duration.minutes(5),
                        statistic="Sum",
                    ),
                    target_group.metrics.http_code_target(
                        elbv2.HttpCodeTarget.TARGET_5XX_COUNT,
                        period=Duration.minutes(5),
                        statistic="Sum",
                    ),
                    target_group.metrics.unhealthy_host_count(
                        statistic="Maximum", period=Duration.minutes(1)
                    ),
                ],
                right=[
                    target_group.metrics.target_response_time(
                        statistic="p95", period=Duration.minutes(5)
                    ),
                ],
                width=12,
                height=6,
            ),
        )

        self._dashboard.add_widgets(
            cw.GraphWidget(
                title="API Gateway — 5xx / 4xx / p99 latency",
                left=[
                    http_api.metric_server_error(period=Duration.minutes(5)),
                    http_api.metric_client_error(period=Duration.minutes(5)),
                ],
                right=[
                    http_api.metric_latency(
                        period=Duration.minutes(5), statistic="p99"
                    ),
                ],
                width=12,
                height=6,
            ),
            cw.GraphWidget(
                title="RDS — CPU / connections / free storage",
                left=[
                    db_instance.metric_cpu_utilization(
                        period=Duration.minutes(5)
                    ),
                    db_instance.metric_database_connections(
                        period=Duration.minutes(5)
                    ),
                ],
                right=[
                    db_instance.metric_free_storage_space(
                        period=Duration.minutes(5)
                    ),
                ],
                width=12,
                height=6,
            ),
        )

        self._dashboard.add_widgets(
            cw.GraphWidget(
                title="SQS — DLQ depth / oldest-message age",
                left=[
                    extraction_dlq.metric_approximate_number_of_messages_visible(
                        period=Duration.minutes(1), statistic="Maximum"
                    ),
                ],
                right=[
                    extraction_queue.metric_approximate_age_of_oldest_message(
                        period=Duration.minutes(5), statistic="Maximum"
                    ),
                ],
                width=12,
                height=6,
            ),
            cw.GraphWidget(
                title="Lambda — errors / throttles / p99 duration",
                left=[
                    extraction_lambda.metric_errors(
                        period=Duration.minutes(5), statistic="Sum"
                    ),
                    extraction_lambda.metric_throttles(
                        period=Duration.minutes(5), statistic="Sum"
                    ),
                ],
                right=[
                    extraction_lambda.metric_duration(
                        period=Duration.minutes(5), statistic="p99"
                    ),
                ],
                width=12,
                height=6,
            ),
        )

        self._dashboard.add_widgets(
            cw.GraphWidget(
                title="Redis — evictions / memory % / engine CPU %",
                left=[
                    self._redis_metric(redis_cluster, "Evictions", "Sum"),
                    self._redis_metric(
                        redis_cluster,
                        "DatabaseMemoryUsagePercentage",
                        "Average",
                    ),
                    self._redis_metric(
                        redis_cluster, "EngineCPUUtilization", "Average"
                    ),
                ],
                width=12,
                height=6,
            ),
        )

        # Alarm-status row: current state of every alarm this stack created.
        self._dashboard.add_widgets(
            cw.AlarmStatusWidget(
                title="All OR-6 alarms",
                alarms=self._alarms,
                width=24,
                height=6,
            )
        )

    # ---------------------------------------------------------------------- #
    # Public properties                                                       #
    # ---------------------------------------------------------------------- #

    @property
    def alerting_topic(self) -> sns.ITopic:
        """The dedicated OR-6 alerting SNS topic.

        Distinct from the APNs "notifications" topic (which is referenced only
        by ARN and never created in infra). Alarms across every tier publish
        to this topic on both the ALARM and OK transitions.
        """
        return self._alerting_topic
