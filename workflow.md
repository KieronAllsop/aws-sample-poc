# Video Encryption and Playback Workflow

This workflow shows how an unencoded, unencrypted MP4 is uploaded, encrypted by AWS MediaConvert, served through CloudFront, and played back in the browser with Shaka Player.

```mermaid
flowchart TD
    A[Unencoded, unencrypted video<br/>MP4 file] -->|Upload| B[S3 Source Bucket]

    B -->|ObjectCreated event<br/>.mp4| C[AWS Lambda<br/>Transcoder Trigger]

    C -->|Create encryption job<br/>with SPEKE URL| D[AWS MediaConvert]

    D -->|Read source video| B
    D -->|SPEKE key request| E[CloudFront HTTPS Edge]

    E -->|Forward /get-clearkey| F[Fargate License Server<br/>FastAPI]
    F -->|Read credentials and ClearKey<br/>from Secrets Manager| G[AWS Secrets Manager]
    F -->|Read/write entitlements| H[Private RDS PostgreSQL]

    D -->|Encrypted DASH/CMAF segments<br/>and MPD| I[S3 Egress Bucket]

    I -->|ObjectCreated event<br/>.mpd| C
    C -->|Patch ClearKey signaling| I

    I -->|Signed origin request<br/>CloudFront OAC| E

    E -->|HTTPS manifest and segments| J[Browser]
    J -->|Shaka Player requests manifest| E
    J -->|ClearKey license request<br/>with key ID| E
    E -->|Forward license request| F
    F -->|Return JWK ClearKey| J

    J -->|Decrypt locally using ClearKey| K[Playback]
```

## Workflow

1. An unencoded, unencrypted MP4 is uploaded to the source S3 bucket.
2. S3 triggers the Lambda transcoder.
3. Lambda starts an AWS MediaConvert job.
4. MediaConvert reads the source file and requests encryption keys through the SPEKE license endpoint.
5. The license service retrieves ClearKey data from Secrets Manager and RDS.
6. MediaConvert writes encrypted DASH/CMAF output to the egress S3 bucket.
7. Lambda patches the generated manifest with ClearKey signaling.
8. CloudFront serves the manifest and encrypted segments over HTTPS.
9. Shaka Player requests the ClearKey license using the media key ID.
10. The browser decrypts the encrypted stream locally and plays it.

## Security Boundaries

- The source S3 bucket stores the uploaded unencrypted input.
- MediaConvert performs the encryption before delivery output is published.
- The egress S3 bucket is private and is accessed through CloudFront Origin Access Control.
- Database credentials and ClearKey values are supplied through AWS Secrets Manager.
- The browser receives encrypted media and a ClearKey license response, then performs local decryption.
