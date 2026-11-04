FROM ghcr.io/blakeblackshear/frigate@sha256:fd60f99ffd4eb6c42360ed4e14953e270586f239309ab3983e55b2e97ffac09a

COPY patch.py test_patch.py /tmp/frigate-video-fixes/
RUN mkdir -p /tmp/frigate-video-native \
    && cp /opt/frigate/frigate/api/media.py /tmp/frigate-video-native/media.py \
    && cp /usr/local/nginx/conf/nginx.conf /tmp/frigate-video-native/nginx.conf \
    && FRIGATE_VIDEO_SOURCE_DIR=/tmp/frigate-video-native \
        python3 -m unittest discover -s /tmp/frigate-video-fixes -p 'test_*.py' \
    && python3 /tmp/frigate-video-fixes/patch.py \
        /opt/frigate/frigate/api/media.py \
        /usr/local/nginx/conf/nginx.conf \
    && rm -rf /tmp/frigate-video-fixes /tmp/frigate-video-native
