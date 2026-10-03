"""
ApiStack: ECS Fargate service, Application Load Balancer, API Gateway HTTP API,
and ECR repository for the RecipeMate Flask API.
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Stack,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_iam as iam,
    aws_logs as logs,
)
from constructs import Construct


class ApiStack(Stack):
    """
    Creates:
      - An ECR repository for the Flask API Docker image.
      - A VPC with public and private subnets for the ECS cluster and ALB.
      - An ECS Fargate cluster (no EC2 instances).
      - A Fargate task definition with the Flask API container (port 5000).
      - An ECS Fargate service with auto-scaling targeting 70% CPU utilisation.
      - An Application Load Balancer (internet-facing) in front of the service.
      - An API Gateway HTTP API that proxies all traffic to the ALB.

    Properties exposed for other stacks:
      - ``ecr_repo``  – the ECR Repository.
      - ``cluster``   – the ECS Cluster.
      - ``service``   – the ECS FargateService.
      - ``api``       – the API Gateway HttpApi.
    """

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # ------------------------------------------------------------------ #
        # VPC — public subnets for ALB, private subnets for Fargate tasks    #
        # ------------------------------------------------------------------ #
        self._vpc = ec2.Vpc(
            self,
            "ApiVpc",
            ip_addresses=ec2.IpAddresses.cidr("10.1.0.0/16"),
            max_azs=2,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
            nat_gateways=1,
        )

        # ------------------------------------------------------------------ #
        # ECR Repository                                                      #
        # ------------------------------------------------------------------ #
        self._ecr_repo = ecr.Repository(
            self,
            "ApiRepository",
            repository_name=f"{self.stack_name.lower()}-flask-api",
            # Keep the last 10 images; remove untagged images after 1 day.
            lifecycle_rules=[
                ecr.LifecycleRule(
                    description="Retain last 10 tagged images",
                    max_image_count=10,
                    tag_status=ecr.TagStatus.TAGGED,
                    # A TAGGED rule must declare which tags it matches; a single
                    # "*" pattern matches every tagged image so the rule keeps
                    # the most recent 10 regardless of tag.
                    tag_pattern_list=["*"],
                    rule_priority=1,
                ),
                ecr.LifecycleRule(
                    description="Expire untagged images after 1 day",
                    max_image_age=Duration.days(1),
                    tag_status=ecr.TagStatus.UNTAGGED,
                    rule_priority=2,
                ),
            ],
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------ #
        # ECS Cluster                                                         #
        # ------------------------------------------------------------------ #
        self._cluster = ecs.Cluster(
            self,
            "ApiCluster",
            vpc=self._vpc,
            cluster_name=f"{self.stack_name}-cluster",
            # No EC2 capacity — Fargate only.
            container_insights=True,
        )

        # ------------------------------------------------------------------ #
        # CloudWatch Log Group for the container                              #
        # ------------------------------------------------------------------ #
        log_group = logs.LogGroup(
            self,
            "ApiLogGroup",
            log_group_name=f"/ecs/{self.stack_name}-flask-api",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------ #
        # Task Execution Role (allows ECR pulls and CloudWatch log writes)   #
        # ------------------------------------------------------------------ #
        execution_role = iam.Role(
            self,
            "TaskExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )

        # ------------------------------------------------------------------ #
        # Task Role (application-level AWS permissions)                      #
        # ------------------------------------------------------------------ #
        task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )

        # Allow the task to read SSM parameters and Secrets Manager secrets.
        task_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "AmazonSSMReadOnlyAccess"
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=["*"],
            )
        )

        # ------------------------------------------------------------------ #
        # Fargate Task Definition (staging sizing: 256 CPU, 512 MiB)         #
        # ------------------------------------------------------------------ #
        self._task_definition = ecs.FargateTaskDefinition(
            self,
            "ApiTaskDefinition",
            cpu=256,
            memory_limit_mib=512,
            execution_role=execution_role,
            task_role=task_role,
        )

        # Container image — uses the ECR repository built from the Dockerfile.
        container = self._task_definition.add_container(
            "FlaskApiContainer",
            image=ecs.ContainerImage.from_ecr_repository(
                self._ecr_repo, tag="latest"
            ),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="flask-api",
                log_group=log_group,
            ),
            environment={
                # Non-sensitive runtime configuration.
                "FLASK_ENV": "staging",
                "PORT": "5000",
            },
            # DATABASE_URL is supplied from Secrets Manager at runtime via the
            # task role permissions above.  The application reads it from the
            # environment; the secret value must be injected by the deployment
            # pipeline or added as a container secret reference.
            health_check=ecs.HealthCheck(
                command=[
                    "CMD-SHELL",
                    "curl -f http://localhost:5000/health || exit 1",
                ],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(60),
            ),
        )

        container.add_port_mappings(
            ecs.PortMapping(
                container_port=5000,
                protocol=ecs.Protocol.TCP,
            )
        )

        # ------------------------------------------------------------------ #
        # Security Groups                                                     #
        # ------------------------------------------------------------------ #
        alb_sg = ec2.SecurityGroup(
            self,
            "AlbSecurityGroup",
            vpc=self._vpc,
            description="Allow HTTP/HTTPS inbound to the ALB",
            allow_all_outbound=True,
        )
        alb_sg.add_ingress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(80),
            description="HTTP from internet",
        )
        alb_sg.add_ingress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(443),
            description="HTTPS from internet",
        )

        service_sg = ec2.SecurityGroup(
            self,
            "ServiceSecurityGroup",
            vpc=self._vpc,
            description="Allow traffic from ALB to Fargate tasks on port 5000",
            allow_all_outbound=True,
        )
        service_sg.add_ingress_rule(
            peer=alb_sg,
            connection=ec2.Port.tcp(5000),
            description="From ALB to Flask container",
        )

        # ------------------------------------------------------------------ #
        # ECS Fargate Service                                                 #
        # ------------------------------------------------------------------ #
        self._service = ecs.FargateService(
            self,
            "ApiService",
            cluster=self._cluster,
            task_definition=self._task_definition,
            desired_count=1,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[service_sg],
            # Allow new deployments to temporarily exceed the desired count.
            min_healthy_percent=50,
            max_healthy_percent=200,
            # Enable ECS managed tags and propagate task tags for cost tracking.
            enable_ecs_managed_tags=True,
            propagate_tags=ecs.PropagatedTagSource.SERVICE,
        )

        # ------------------------------------------------------------------ #
        # Auto-scaling — target 70% CPU utilisation                          #
        # ------------------------------------------------------------------ #
        scalable_target = self._service.auto_scale_task_count(
            min_capacity=1,
            max_capacity=4,
        )

        scalable_target.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=70,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(30),
        )

        # ------------------------------------------------------------------ #
        # Application Load Balancer                                          #
        # ------------------------------------------------------------------ #
        self._alb = elbv2.ApplicationLoadBalancer(
            self,
            "ApiAlb",
            vpc=self._vpc,
            internet_facing=True,
            security_group=alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            load_balancer_name=f"{self.stack_name}-alb",
        )

        # HTTP listener — forward to the Fargate service on port 5000.
        http_listener = self._alb.add_listener(
            "HttpListener",
            port=80,
            open=False,  # Security group already permits port 80.
        )

        http_listener.add_targets(
            "FlaskServiceTarget",
            port=5000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[self._service],
            health_check=elbv2.HealthCheck(
                path="/health",
                healthy_http_codes="200",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
            deregistration_delay=Duration.seconds(30),
        )

        # ------------------------------------------------------------------ #
        # API Gateway HTTP API → ALB                                         #
        # ------------------------------------------------------------------ #
        alb_integration = apigwv2_integrations.HttpAlbIntegration(
            "AlbIntegration",
            listener=http_listener,
            # Forward all methods and paths to the ALB.
            method=apigwv2.HttpMethod.ANY,
            parameter_mapping=apigwv2.ParameterMapping()
            .overwrite_header(
                "X-Forwarded-Proto",
                apigwv2.MappingValue.custom("https"),
            ),
        )

        self._api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name=f"{self.stack_name}-api",
            description="RecipeMate Flask API via ECS Fargate",
            # Proxy all paths and methods to the ALB.
            default_integration=alb_integration,
            # CORS is handled by the Flask app; no additional API Gateway CORS
            # configuration needed here.
            cors_preflight=None,
        )

        # ------------------------------------------------------------------ #
        # CloudFormation Outputs                                              #
        # ------------------------------------------------------------------ #
        cdk.CfnOutput(
            self,
            "EcrRepositoryUri",
            value=self._ecr_repo.repository_uri,
            description="ECR repository URI for the Flask API image",
        )

        cdk.CfnOutput(
            self,
            "EcsClusterName",
            value=self._cluster.cluster_name,
            description="ECS cluster name",
        )

        cdk.CfnOutput(
            self,
            "AlbDnsName",
            value=self._alb.load_balancer_dns_name,
            description="ALB DNS name",
        )

        cdk.CfnOutput(
            self,
            "ApiEndpoint",
            value=self._api.api_endpoint,
            description="API Gateway HTTP API endpoint URL",
        )

    # ---------------------------------------------------------------------- #
    # Public properties                                                       #
    # ---------------------------------------------------------------------- #

    @property
    def ecr_repo(self) -> ecr.Repository:
        """The ECR repository holding the Flask API Docker image."""
        return self._ecr_repo

    @property
    def cluster(self) -> ecs.Cluster:
        """The ECS Fargate cluster."""
        return self._cluster

    @property
    def service(self) -> ecs.FargateService:
        """The ECS Fargate service running the Flask API."""
        return self._service

    @property
    def api(self) -> apigwv2.HttpApi:
        """The API Gateway HTTP API fronting the Fargate service."""
        return self._api
