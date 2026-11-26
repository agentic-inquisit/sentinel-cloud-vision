## Kafka Topic Topology   

1. raw-visual-feed:
    - Description: High-frequency byte streams from visual sources (RTSP/Webcam).
    - Partitions: 5+ (Scalable based on throughput).
    - Cleanup Policy: delete (Retention: 24h).

2. inference-results:
    - Description: Enriched JSON data post-AI inference.
    - Partitions: 3.
    - Cleanup Policy: compact (Keep latest result per stream_id).

3. system-alerts:
    - Description: Real-time triggers for model drift or critical events.
    - Partitions: 1.
    - Backup: cpy. 
    - Cleanup Policy: delete.
