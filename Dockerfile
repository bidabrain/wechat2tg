# syntax=docker/dockerfile:1
#
# wechat2tg —— 统一镜像
#   Linux 桌面版微信(常驻) + 消息采集(解密数据库) + Telegram 转发 + 管理面板
#   无需安卓模拟器 / KVM，纯容器运行。
#
#   docker build -t wechat2tg .
#
# 可选构建参数：
#   WECHAT_DEB  微信 Linux 版 deb 包地址（默认官方 x86_64）

FROM ubuntu:24.04

ARG WECHAT_DEB="https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_x86_64.deb"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    TZ=Asia/Shanghai \
    DISPLAY=:1 \
    VNC_PORT=5900 \
    WEB_PORT=8080 \
    DATA_DIR=/data \
    WECHAT_HOME=/data/home \
    SCREEN_GEOMETRY=1280x800x24

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl locales tzdata supervisor procps psmisc \
      xvfb x11vnc novnc websockify openbox xdotool xclip x11-utils xauth dbus-x11 \
      scrot tesseract-ocr tesseract-ocr-chi-sim \
      fonts-noto-cjk fonts-noto-color-emoji \
      python3 python3-venv sqlite3 inotify-tools \
      libatomic1 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
      libxcb-render-util0 libxcb-randr0 libxcb-shape0 libxcb-xfixes0 libxcb-xkb1 \
      libxcb-cursor0 libxcb-sync1 libnss3 libgbm1 libasound2t64 libgtk-3-0t64 libpulse0 \
  && sed -i 's/# zh_CN.UTF-8/zh_CN.UTF-8/' /etc/locale.gen && locale-gen \
  && curl -fL -o /tmp/wechat.deb "${WECHAT_DEB}" \
  && apt-get install -y /tmp/wechat.deb && rm /tmp/wechat.deb \
  && rm -rf /var/lib/apt/lists/*

# 管理面板 + 采集/桥接依赖
COPY manager/requirements.txt /opt/manager/requirements.txt
RUN python3 -m venv /opt/manager/venv \
 && /opt/manager/venv/bin/pip install --no-cache-dir -r /opt/manager/requirements.txt
COPY manager/ /opt/manager/

COPY docker/supervisord.conf /etc/supervisor/wx.conf
COPY docker/*.sh /opt/scripts/
RUN sed -i 's/\r$//' /opt/scripts/*.sh /etc/supervisor/wx.conf \
 && chmod +x /opt/scripts/*.sh

VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD curl -fsS "http://127.0.0.1:${WEB_PORT}/healthz" || exit 1
ENTRYPOINT ["/opt/scripts/entrypoint.sh"]
