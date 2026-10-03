"""
CacheStack: ElastiCache Redis cluster placed in private VPC subnets, intended
for caching LLM search results, presigned-upload metadata, and other
short-lived application state.
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    aws_elasticache as elasticache,
)
from constructs import Construct


class CacheStack(Stack):
    """
    Creates:
      - A VPC with isolated private subnets (reused from a caller if supplied,
        otherwise a small dedicated VPC with no NAT gateway).
      - A security group that allows inbound Redis traffic (port 6379) from
        within the VPC CIDR.
      - An ElastiCache subnet group built from the VPC's isolated/private
        subnet IDs.
      - A single-node ElastiCache Redis cluster.

    Properties exposed for other stacks:
      - ``vpc``            – the VPC containing the cache.
      - ``redis``          – the ElastiCache CfnCacheCluster.
      - ``redis_endpoint`` – the primary Redis endpoint address.
      - ``security_group`` – the security group guarding the cluster.
    """

    REDIS_PORT: int = 6379

    def __init__(
        self,
        scope: Construct,
        id: str,
        vpc: ec2.Vpc | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        # ------------------------------------------------------------------ #
        # VPC — reuse the caller's VPC, or create a small isolated one.       #
        # ------------------------------------------------------------------ #
        if vpc is not None:
            self._vpc = vpc
        else:
            self._vpc = ec2.Vpc(
                self,
                "Vpc",
                ip_addresses=ec2.IpAddresses.cidr("10.2.0.0/16"),
                max_azs=2,
                subnet_configuration=[
                    ec2.SubnetConfiguration(
                        name="Isolated",
                        subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                        cidr_mask=24,
                    )
                ],
                nat_gateways=0,
            )

        # ------------------------------------------------------------------ #
        # Security group — allow Redis (6379) from within the VPC CIDR.       #
        # ------------------------------------------------------------------ #
        self._redis_sg = ec2.SecurityGroup(
            self,
            "RedisSecurityGroup",
            vpc=self._vpc,
            description="Allow Redis access from within the VPC",
            allow_all_outbound=False,
        )

        self._redis_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(self._vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(self.REDIS_PORT),
            description="Redis from VPC CIDR",
        )

        # ------------------------------------------------------------------ #
        # Subnet group — built from the isolated/private subnet IDs.          #
        # ------------------------------------------------------------------ #
        # Prefer isolated subnets; fall back to private subnets when none exist.
        subnets = self._vpc.isolated_subnets or self._vpc.private_subnets
        subnet_ids = [subnet.subnet_id for subnet in subnets]

        self._subnet_group = elasticache.CfnSubnetGroup(
            self,
            "RedisSubnetGroup",
            description="Subnet group for the RecipeMate Redis cluster",
            subnet_ids=subnet_ids,
        )

        # ------------------------------------------------------------------ #
        # ElastiCache Redis — single node, burstable Graviton instance.       #
        # ------------------------------------------------------------------ #
        # Node type is context-driven so prod can override via CDK context.
        cache_node_type: str = (
            self.node.try_get_context("redisNodeType") or "cache.t4g.micro"
        )

        self._redis = elasticache.CfnCacheCluster(
            self,
            "RedisCluster",
            engine="redis",
            cache_node_type=cache_node_type,
            num_cache_nodes=1,
            port=self.REDIS_PORT,
            cache_subnet_group_name=self._subnet_group.ref,
            vpc_security_group_ids=[self._redis_sg.security_group_id],
        )
        # The subnet group must exist before the cluster references it.
        self._redis.add_dependency(self._subnet_group)

        # ------------------------------------------------------------------ #
        # Outputs                                                             #
        # ------------------------------------------------------------------ #
        cdk.CfnOutput(
            self,
            "RedisEndpoint",
            value=self._redis.attr_redis_endpoint_address,
            description="ElastiCache Redis primary endpoint hostname",
        )

        cdk.CfnOutput(
            self,
            "RedisPort",
            value=self._redis.attr_redis_endpoint_port,
            description="ElastiCache Redis endpoint port",
        )

    # ---------------------------------------------------------------------- #
    # Public properties                                                       #
    # ---------------------------------------------------------------------- #

    @property
    def vpc(self) -> ec2.Vpc:
        """The VPC that contains the cache cluster."""
        return self._vpc

    @property
    def redis(self) -> elasticache.CfnCacheCluster:
        """The ElastiCache Redis cluster."""
        return self._redis

    @property
    def redis_endpoint(self) -> str:
        """The primary Redis endpoint address (resolved at deploy time)."""
        return self._redis.attr_redis_endpoint_address

    @property
    def security_group(self) -> ec2.SecurityGroup:
        """The security group guarding the Redis cluster."""
        return self._redis_sg
