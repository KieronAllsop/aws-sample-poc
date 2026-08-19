import os
import re
import boto3
import urllib.parse

CLEAR_KEY_SYSTEM_ID = "urn:uuid:e2719d58-a985-b3c9-781a-b030af78d30e"
CLEAR_KEY_PATCH_VERSION = "v5"


def patch_clear_key_manifest(s3, bucket, key):
    metadata = s3.head_object(Bucket=bucket, Key=key).get("Metadata", {})
    if metadata.get("clearkey-patched") == CLEAR_KEY_PATCH_VERSION:
        return

    response = s3.get_object(Bucket=bucket, Key=key)
    manifest = response["Body"].read().decode("utf-8")
    kid_match = re.search(r'cenc:default_KID="([0-9a-fA-F-]+)"', manifest)
    if not kid_match:
        raise ValueError("Generated manifest does not contain a default KID")
    kid = kid_match.group(1)
    signaling = (
        f'<ContentProtection cenc:default_KID="{kid}" '
        f'schemeIdUri="{CLEAR_KEY_SYSTEM_ID}" value="ClearKey1.0"/>'
    )

    manifest = re.sub(
        r'\s*<ContentProtection[^>]*schemeIdUri="urn:uuid:(?:e2719d58-a985-b3c9-781a-b030af78d30e|1077efec-c0b2-4d02-ace3-3c1e52e2fb4b)"[^>]*/>',
        '',
        manifest
    )
    manifest = manifest.replace(
        ' value="cenc"/>',
        f' value="cenc"/>\n      {signaling}'
    )

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=manifest.encode("utf-8"),
        ContentType="application/dash+xml",
        Metadata={"clearkey-patched": CLEAR_KEY_PATCH_VERSION}
    )

def lambda_handler(event, context):
    # 1. Parse the incoming S3 upload notification metadata
    source_bucket = event['Records'][0]['s3']['bucket']['name']
    source_key = urllib.parse.unquote_plus(event['Records'][0]['s3']['object']['key'])
    s3 = boto3.client('s3')

    if source_bucket == os.environ['EGRESS_BUCKET_NAME'] and source_key.endswith('.mpd'):
        patch_clear_key_manifest(s3, source_bucket, source_key)
        return {"statusCode": 200, "message": "ClearKey manifest patched"}
    
    # 2. Derive file output names (e.g., stripping the '.mp4' file extension)
    output_prefix = os.path.splitext(source_key)[0]
    destination_bucket = os.environ['EGRESS_BUCKET_NAME']
    
    # 3. Instantiate the regional media processing client engine
    mediaconvert = boto3.client('mediaconvert', endpoint_url=os.environ['MEDIACONVERT_ENDPOINT'])
    
    # 4. Build the MediaConvert job configuration matrix for DASH/CMAF
    job_specification = {
        "Role": os.environ['MEDIACONVERT_ROLE_ARN'],
        "Settings": {
            "Inputs": [{
                "FileInput": f"s3://{source_bucket}/{source_key}",
                "AudioSelectors": {
                    "Audio Selector 1": {
                        "DefaultSelection": "DEFAULT"
                    }
                }
            }],
            "OutputGroups": [{
                "Name": "DASH ISO Encryption Stream",
                "CustomName": "dash_clearkey",
                "OutputGroupSettings": {
                    "Type": "DASH_ISO_GROUP_SETTINGS",
                    "DashIsoGroupSettings": {
                        "SegmentLength": 6,
                        "FragmentLength": 2,
                        "Destination": f"s3://{destination_bucket}/{output_prefix}/",
                        "Encryption": {
                            # --- FIXED ENUM VALUE HERE ---
                            "PlaybackDeviceCompatibility": "CENC_V1", 
                            "SpekeKeyProvider": {
                                "ResourceId": "YourUniqueVideoID",
                                "SystemIds": [
                                    "1077efec-c0b2-4d02-ace3-3c1e52e2fb4b", # ClearKey System ID
                                    "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"  # Widevine System ID
                                ],
                                "Url": os.environ["SPEKE_LICENSE_SERVER_URL"]
                            }
                        }
                    }
                },
                "Outputs": [
                    {
                        "ContainerSettings": {"Container": "MPD"},
                        "VideoDescription": {
                            "CodecSettings": {
                                "Codec": "H_264",
                                "H264Settings": {
                                    "RateControlMode": "QVBR",
                                    "MaxBitrate": 5000000,
                                    "QvbrSettings": {"QvbrQualityLevel": 7}
                                }
                            }
                        },
                        "NameModifier": "_video"
                    },
                    {
                        "ContainerSettings": {"Container": "MPD"},
                        "AudioDescriptions": [{
                            "AudioSourceName": "Audio Selector 1",
                            "CodecSettings": {
                                "Codec": "AAC",
                                "AacSettings": {
                                    "Bitrate": 96000,
                                    "SampleRate": 48000,
                                    "CodingMode": "CODING_MODE_2_0"
                                }
                            }
                        }],
                        "NameModifier": "_audio"
                    }
                ]
            }]
        }
    }

    # 6. Ship the payload down to the automated encoding farm context
    response = mediaconvert.create_job(**job_specification)
    return {"statusCode": 200, "jobId": response['Job']['Id']}

