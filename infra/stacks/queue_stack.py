"""
QueueStack: the asynchronous recipe-extraction pipeline.

An SQS queue receives extraction jobs enqueued by the API.  A Python Lambda
function consumes the queue (batch size 1), downloads the uploaded image from
S3, calls the GPT-4o vision API, and writes the structured result back to the
database.  Messages that fail repeatedly are routed to a dead-letter queue so
a single poison message never blocks the pipeline.
"""
from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Stack,
    aws_iam as iam,
    aws_lambda as aws_lambda,
    aws_lambda_event_sources as lambda_event_sources,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_sqs as sqs,
)
from constructs import Construct

# Lambda timeout — the queue's visibility timeout must exceed this so an
# in-flight message is not redelivered while the function is still running.
LAMBDA_TIMEOUT = Duration.seconds(120)
QUEUE_VISIBILITY_TIMEOUT = Duration.seconds(300)


class QueueStack(Stack):
    """
    Creates:
      - A dead-letter queue retaining failed extraction messages for 14 days.
      - A main extraction queue with a 300s visibility timeout and a redrive
        policy that sends a message to the DLQ after 3 failed receives.
      - A Python 3.11 Lambda function (``extract_handler.handler``) built from
        the ``lambda/`` asset directory, with a 120s timeout and 1024 MB of
        memory, wired to the queue via an SQS event source (batch size 1).
      - IAM grants allowing the Lambda to consume the queue, read from the
        recipe-images S3 bucket, and fetch the OpenAI API key secret.

    Properties exposed for other stacks:
      - ``queue``             – the main extraction SQS Queue.
      - ``dlq``               – the dead-letter SQS Queue.
      - ``extraction_lambda`` – the extraction Lambda Function.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        openai_secret: secretsmanager.ISecret | None = None,
        env_name: str = "staging",
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        # ------------------------------------------------------------------ #
        # Resolve references to resources owned by sibling stacks.           #
        #                                                                    #
        # These are supplied via CDK context (``-c key=value``) or fall back #
        # to conventional names so the stack synthesises standalone.         #
        # ------------------------------------------------------------------ #
        s3_bucket_name: str = (
            self.node.try_get_context("imagesBucketName")
            or f"{self.stack_name.lower()}-recipe-images"
        )
        database_url: str = (
            self.node.try_get_context("databaseUrl")
            or "postgresql://localhost:5432/recipemate"
        )

        images_bucket = s3.Bucket.from_bucket_name(
            self, "RecipeImagesBucket", s3_bucket_name
        )

        # Prefer the OpenAI secret handed in by SecretsStack. Falling back to a
        # by-name lookup keeps the stack synthesisable standalone (e.g. in CI).
        openai_secret_name: str = (
            openai_secret.secret_name
            if openai_secret is not None
            else (
                self.node.try_get_context("openaiSecretName")
                or f"recipemate/{env_name}/openai-api-key"
            )
        )
        if openai_secret is None:
            openai_secret = secretsmanager.Secret.from_secret_name_v2(
                self, "OpenAiApiKeySecret", openai_secret_name
            )
        self._openai_secret = openai_secret

        # ------------------------------------------------------------------ #
        # Dead-letter queue — holds messages that fail extraction 3 times.   #
        # ------------------------------------------------------------------ #
        self._dlq = sqs.Queue(
            self,
            "ExtractionDLQ",
            queue_name=f"{self.stack_name}-extraction-dlq",
            # Keep failed messages around long enough to inspect / replay.
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )

        # ------------------------------------------------------------------ #
        # Main extraction queue — redrives to the DLQ after 3 receives.      #
        # ------------------------------------------------------------------ #
        self._queue = sqs.Queue(
            self,
            "ExtractionQueue",
            queue_name=f"{self.stack_name}-extraction-queue",
            # Must exceed the Lambda timeout so a message is not redelivered
            # while the function is still processing it.
            visibility_timeout=QUEUE_VISIBILITY_TIMEOUT,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=self._dlq,
            ),
        )

        # ------------------------------------------------------------------ #
        # Extraction Lambda function.                                        #
        # ------------------------------------------------------------------ #
        self._extraction_lambda = aws_lambda.Function(
            self,
            "ExtractionFunction",
            function_name=f"{self.stack_name}-extraction",
            runtime=aws_lambda.Runtime.PYTHON_3_11,
            handler="extract_handler.handler",
            # The ``lambda/`` directory lives at the repository root. Resolve it
            # to an absolute path relative to this file (infra/stacks/ -> repo
            # root) so the asset is found regardless of the directory cdk is
            # invoked from.
            code=aws_lambda.Code.from_asset(
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                    "lambda",
                )
            ),
            timeout=LAMBDA_TIMEOUT,
            memory_size=1024,
            environment={
                # Pass the *name* of the secret, not its value. The handler
                # fetches the key from Secrets Manager at runtime via boto3, so
                # the plaintext key never lands in the CloudFormation template
                # or the function's configured environment.
                "OPENAI_SECRET_NAME": openai_secret_name,
                "OPENAI_SECRET_JSON_KEY": "OPENAI_API_KEY",
                "S3_BUCKET": s3_bucket_name,
                "DATABASE_URL": database_url,
            },
            description="Consumes SQS extraction jobs and calls GPT-4o vision",
        )

        # ------------------------------------------------------------------ #
        # Permissions.                                                       #
        # ------------------------------------------------------------------ #
        # Consume messages from the main queue (also wired via the event
        # source below; granting explicitly keeps intent clear).
        self._queue.grant_consume_messages(self._extraction_lambda)

        # Read uploaded images from the recipe-images bucket.
        images_bucket.grant_read(self._extraction_lambda)

        # Fetch the OpenAI API key from Secrets Manager.
        openai_secret.grant_read(self._extraction_lambda)

        # Belt-and-braces: allow reading any Secrets Manager secret the
        # function may need (scoped to GetSecretValue).
        self._extraction_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[openai_secret.secret_arn],
            )
        )

        # ------------------------------------------------------------------ #
        # SQS event source — deliver one message per invocation.            #
        # ------------------------------------------------------------------ #
        self._extraction_lambda.add_event_source(
            lambda_event_sources.SqsEventSource(
                self._queue,
                batch_size=1,
            )
        )

        # ------------------------------------------------------------------ #
        # CloudFormation Outputs                                             #
        # ------------------------------------------------------------------ #
        cdk.CfnOutput(
            self,
            "ExtractionQueueUrl",
            value=self._queue.queue_url,
            description="URL of the main extraction SQS queue",
        )
        cdk.CfnOutput(
            self,
            "ExtractionQueueArn",
            value=self._queue.queue_arn,
            description="ARN of the main extraction SQS queue",
        )
        cdk.CfnOutput(
            self,
            "ExtractionDLQUrl",
            value=self._dlq.queue_url,
            description="URL of the extraction dead-letter queue",
        )
        cdk.CfnOutput(
            self,
            "ExtractionLambdaArn",
            value=self._extraction_lambda.function_arn,
            description="ARN of the recipe-extraction Lambda function",
        )

    # ---------------------------------------------------------------------- #
    # Public properties                                                       #
    # ---------------------------------------------------------------------- #

    @property
    def queue(self) -> sqs.Queue:
        """The main SQS queue carrying extraction jobs."""
        return self._queue

    @property
    def dlq(self) -> sqs.Queue:
        """The dead-letter queue holding messages that failed extraction."""
        return self._dlq

    @property
    def extraction_lambda(self) -> aws_lambda.Function:
        """The Lambda function that consumes the queue and runs extraction."""
        return self._extraction_lambda

    @property
    def openai_secret(self):
        """The Secrets Manager secret holding the OpenAI API key."""
        return self._openai_secret
