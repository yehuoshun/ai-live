// AI 皮套渲染 + 音频驱动口型（Canvas 2D，零依赖）
// 结构：AudioBus(取 RMS) -> Puppet(绘制) -> Bridge(SSE 收播报)
// 换真 Live2D 时只需实现同一套适配器接口：setMouth(v) / setSpeaking(b) / setText(t) / tick(dt)
(function () {
  'use strict';

  var params = new URLSearchParams(location.search);
  var CLEAN = params.get('clean') === '1';
  var MOUTH_GAIN = Number(params.get('gain') || 1.9);
  if (CLEAN) document.body.classList.add('clean');

  var W = 1080, H = 1350;

  var state = {
    mouth: 0, speaking: false, blink: 0, breath: 0, sway: 0,
    gaze: { x: 0, y: 0 }, tilt: 0, nextBlinkAt: 0, energy: 0
  };

  var audio = {
    ctx: null, analyser: null, buf: null, queue: [], playing: false,
    ensure: function () {
      if (this.ctx) return this.ctx;
      var AC = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AC();
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.analyser.smoothingTimeConstant = 0.55;
      this.analyser.connect(this.ctx.destination);
      this.buf = new Uint8Array(this.analyser.fftSize);
      return this.ctx;
    },
    resume: function () {
      this.ensure();
      if (this.ctx.state === 'suspended') return this.ctx.resume();
      return Promise.resolve();
    },
    level: function () {
      if (!this.analyser) return 0;
      this.analyser.getByteTimeDomainData(this.buf);
      var sum = 0, n = this.buf.length;
      for (var i = 0; i < n; i++) { var v = (this.buf[i] - 128) / 128; sum += v * v; }
      return Math.sqrt(sum / n);
    },
    push: function (url, text) {
      this.queue.push({ url: url, text: text });
      if (!this.playing) this.next();
    },
    next: function () {
      var self = this;
      if (!this.queue.length) { this.playing = false; Bridge.onIdle(); return; }
      this.playing = true;
      var item = this.queue.shift();
      this.ensure();
      self.resume().then(function () { return fetch(item.url); })
        .then(function (r) { if (!r.ok) throw new Error('音频 ' + r.status); return r.arrayBuffer(); })
        .then(function (ab) { return self.ctx.decodeAudioData(ab); })
        .then(function (buf) {
          var src = self.ctx.createBufferSource();
          src.buffer = buf;
          src.connect(self.analyser);
          src.onended = function () { self.next(); };
          Bridge.onStart(item.text, buf.duration);
          src.start();
        }).catch(function (e) {
          console.warn('[audio]', e.message);
          Bridge.setChip('音频失败: ' + e.message, 'bad');
          self.next();
        });
    }
  };

  var canvas = document.getElementById('stage');
  var ctx = canvas.getContext('2d');
  var view = { scale: 1, dx: 0, dy: 0 };

  function resize() {
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var cw = window.innerWidth, ch = window.innerHeight;
    canvas.width = Math.floor(cw * dpr);
    canvas.height = Math.floor(ch * dpr);
    canvas.style.width = cw + 'px';
    canvas.style.height = ch + 'px';
    var s = Math.min(cw / W, ch / H);
    view.scale = s * dpr;
    view.dx = (cw * dpr - W * s * dpr) / 2;
    view.dy = (ch * dpr - H * s * dpr) / 2;
  }
  window.addEventListener('resize', resize);

  function rr(c, x, y, w, h, r) {
    c.beginPath();
    c.moveTo(x + r, y);
    c.arcTo(x + w, y, x + w, y + h, r);
    c.arcTo(x + w, y + h, x, y + h, r);
    c.arcTo(x, y + h, x, y, r);
    c.arcTo(x, y, x + w, y, r);
    c.closePath();
  }

  var SKIN = '#ffe3d6', SKIN_D = '#f2c3af', HAIR = '#3a3050', HAIR_L = '#5b4d78';
  var CLOTH = '#4a5c85', CLOTH_D = '#2a3550';

  function drawPuppet() {
    var m = state.mouth;
    var breathe = Math.sin(state.breath) * 5;
    var sway = Math.sin(state.sway);
    var headX = 540 + sway * 8;
    var headY = 490 + breathe * 0.5;
    var tilt = state.tilt + sway * 0.01;
    var eyeOpen = 1 - state.blink;

    ctx.save();
    ctx.translate(view.dx, view.dy);
    ctx.scale(view.scale, view.scale);
    ctx.clearRect(-view.dx / view.scale, -view.dy / view.scale, W + 2, H + 2);

    // 后发（先画，下半截被身体压住）
    ctx.save();
    ctx.translate(headX, headY);
    ctx.rotate(tilt);
    ctx.fillStyle = HAIR;
    ctx.beginPath();
    ctx.moveTo(0, -336);
    ctx.bezierCurveTo(214, -336, 330, -196, 322, 30);
    ctx.bezierCurveTo(316, 250, 336, 520, 276, 720);
    ctx.quadraticCurveTo(140, 776, 0, 748);
    ctx.quadraticCurveTo(-140, 776, -276, 720);
    ctx.bezierCurveTo(-336, 520, -316, 250, -322, 30);
    ctx.bezierCurveTo(-330, -196, -214, -336, 0, -336);
    ctx.closePath();
    ctx.fill();
    ctx.restore();

    // 身体
    ctx.save();
    ctx.translate(0, breathe);
    var cg = ctx.createLinearGradient(0, 820, 0, 1350);
    cg.addColorStop(0, CLOTH);
    cg.addColorStop(1, CLOTH_D);
    ctx.fillStyle = cg;
    ctx.beginPath();
    ctx.moveTo(540 - 336, 1350);
    ctx.bezierCurveTo(540 - 326, 1010, 540 - 224, 872, 540 - 104, 838);
    ctx.quadraticCurveTo(540, 884, 540 + 104, 838);
    ctx.bezierCurveTo(540 + 224, 872, 540 + 326, 1010, 540 + 336, 1350);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = '#f2f5ff';
    ctx.beginPath();
    ctx.moveTo(540 - 108, 830);
    ctx.quadraticCurveTo(540 - 60, 900, 540, 918);
    ctx.quadraticCurveTo(540 + 60, 900, 540 + 108, 830);
    ctx.quadraticCurveTo(540 + 40, 856, 540, 856);
    ctx.quadraticCurveTo(540 - 40, 856, 540 - 108, 830);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = '#e2607d';
    ctx.beginPath();
    ctx.ellipse(540, 906, 32, 24, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();

    // 脖子
    ctx.fillStyle = SKIN_D;
    rr(ctx, 540 - 54, 690, 108, 150, 40);
    ctx.fill();

    // 头
    ctx.save();
    ctx.translate(headX, headY);
    ctx.rotate(tilt);

    [-1, 1].forEach(function (s) {
      ctx.fillStyle = SKIN;
      ctx.beginPath();
      ctx.ellipse(s * 246, 44, 28, 44, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = 'rgba(210,150,138,.45)';
      ctx.lineWidth = 5;
      ctx.beginPath();
      ctx.ellipse(s * 246, 44, 12, 23, 0, 0, Math.PI * 2);
      ctx.stroke();
    });

    var fg = ctx.createRadialGradient(0, -50, 40, 0, 30, 300);
    fg.addColorStop(0, '#fff1ea');
    fg.addColorStop(0.72, SKIN);
    fg.addColorStop(1, SKIN_D);
    ctx.fillStyle = fg;
    ctx.beginPath();
    ctx.moveTo(0, -262);
    ctx.bezierCurveTo(172, -262, 250, -140, 250, 24);
    ctx.bezierCurveTo(250, 168, 132, 268, 0, 268);
    ctx.bezierCurveTo(-132, 268, -250, 168, -250, 24);
    ctx.bezierCurveTo(-250, -140, -172, -262, 0, -262);
    ctx.closePath();
    ctx.fill();

    [-1, 1].forEach(function (s) {
      var g = ctx.createRadialGradient(s * 168, 138, 4, s * 168, 138, 66);
      g.addColorStop(0, 'rgba(255,132,146,.5)');
      g.addColorStop(1, 'rgba(255,132,146,0)');
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.ellipse(s * 168, 138, 66, 42, 0, 0, Math.PI * 2);
      ctx.fill();
    });

    [-1, 1].forEach(function (s) {
      ctx.save();
      ctx.translate(s * 110, 26);
      ctx.scale(1, Math.max(0.05, eyeOpen));
      ctx.fillStyle = '#fffefd';
      ctx.beginPath();
      ctx.ellipse(0, 0, 58, 64, 0, 0, Math.PI * 2);
      ctx.fill();
      var ig = ctx.createRadialGradient(0, 14, 6, 0, 8, 48);
      ig.addColorStop(0, '#a68cff');
      ig.addColorStop(0.55, '#6246cf');
      ig.addColorStop(1, '#2a1d63');
      ctx.fillStyle = ig;
      ctx.beginPath();
      ctx.ellipse(state.gaze.x * 12, 6 + state.gaze.y * 8, 42, 48, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#140e2c';
      ctx.beginPath();
      ctx.ellipse(state.gaze.x * 12, 8 + state.gaze.y * 8, 18, 23, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = 'rgba(255,255,255,.94)';
      ctx.beginPath();
      ctx.ellipse(-15 + state.gaze.x * 6, -16, 13, 15, -0.4, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = 'rgba(255,255,255,.5)';
      ctx.beginPath();
      ctx.ellipse(17, 24, 6, 7, 0.3, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = '#241a38';
      ctx.lineWidth = 10;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.arc(0, 8, 60, Math.PI * 1.04, Math.PI * 1.96);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(s * 56, -22);
      ctx.quadraticCurveTo(s * 74, -34, s * 82, -48);
      ctx.stroke();
      ctx.restore();

      ctx.strokeStyle = '#4c4064';
      ctx.lineWidth = 8;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(s * 70, -62 - m * 6);
      ctx.quadraticCurveTo(s * 112, -84 - m * 8, s * 152, -60 - m * 4);
      ctx.stroke();
    });

    ctx.strokeStyle = 'rgba(198,140,126,.7)';
    ctx.lineWidth = 6;
    ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.moveTo(-3, 112);
    ctx.quadraticCurveTo(9, 124, -3, 132);
    ctx.stroke();

    var mw = 28 + 34 * m;
    var mh = 5 + 48 * m;
    var my = 176;
    if (m < 0.05) {
      ctx.strokeStyle = '#c8697a';
      ctx.lineWidth = 8;
      ctx.beginPath();
      ctx.moveTo(-22, my - 2);
      ctx.quadraticCurveTo(0, my + 14, 22, my - 2);
      ctx.stroke();
    } else {
      var mg = ctx.createLinearGradient(0, my - mh, 0, my + mh);
      mg.addColorStop(0, '#8e3044');
      mg.addColorStop(1, '#571325');
      ctx.fillStyle = mg;
      ctx.beginPath();
      ctx.ellipse(0, my, mw, mh, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = 'rgba(255,255,255,.92)';
      ctx.beginPath();
      ctx.ellipse(0, my - mh * 0.7, mw * 0.78, Math.max(2, mh * 0.24), 0, 0, Math.PI * 2);
      ctx.fill();
      if (m > 0.34) {
        ctx.fillStyle = '#e07d88';
        ctx.beginPath();
        ctx.ellipse(0, my + mh * 0.52, mw * 0.58, mh * 0.34, 0, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.strokeStyle = '#a8434f';
      ctx.lineWidth = 5;
      ctx.beginPath();
      ctx.ellipse(0, my, mw, mh, 0, 0, Math.PI * 2);
      ctx.stroke();
    }

    // 前发（刘海）
    ctx.fillStyle = HAIR;
    ctx.beginPath();
    ctx.moveTo(-252, 10);
    ctx.bezierCurveTo(-262, -206, -150, -302, 0, -302);
    ctx.bezierCurveTo(150, -302, 262, -206, 252, 10);
    ctx.lineTo(214, -44);
    ctx.quadraticCurveTo(178, -128, 126, -152);
    ctx.quadraticCurveTo(74, -108, 32, -168);
    ctx.quadraticCurveTo(-18, -116, -66, -158);
    ctx.quadraticCurveTo(-128, -118, -184, -146);
    ctx.lineTo(-252, 10);
    ctx.closePath();
    ctx.fill();

    ctx.strokeStyle = 'rgba(150,132,190,.5)';
    ctx.lineWidth = 7;
    ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.moveTo(-152, -214);
    ctx.quadraticCurveTo(-64, -256, 44, -242);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(116, -232);
    ctx.quadraticCurveTo(178, -204, 206, -152);
    ctx.stroke();

    // 侧发
    [-1, 1].forEach(function (s) {
      ctx.fillStyle = HAIR;
      ctx.beginPath();
      ctx.moveTo(s * 250, -40);
      ctx.bezierCurveTo(s * 268, 120, s * 252, 280, s * 216, 400);
      ctx.quadraticCurveTo(s * 178, 420, s * 168, 372);
      ctx.bezierCurveTo(s * 200, 250, s * 214, 110, s * 198, -30);
      ctx.closePath();
      ctx.fill();
      ctx.fillStyle = HAIR_L;
      ctx.beginPath();
      ctx.ellipse(s * 210, 60, 7, 40, s * 0.08, 0, Math.PI * 2);
      ctx.fill();
    });

    ctx.strokeStyle = HAIR;
    ctx.lineWidth = 11;
    ctx.beginPath();
    ctx.moveTo(8, -300);
    ctx.quadraticCurveTo(44, -364 + sway * 8, 100, -352);
    ctx.stroke();

    ctx.restore();
    ctx.restore();
  }

  var last = 0;
  function frame(now) {
    var dt = last ? Math.min(0.05, (now - last) / 1000) : 0.016;
    last = now;
    var t = now / 1000;
    state.breath += dt * 1.5;
    state.sway += dt * 0.9;
    if (now > state.nextBlinkAt) {
      state.blink = 1;
      state.nextBlinkAt = now + 2600 + Math.random() * 3600;
      setTimeout(function () { state.blink = 0; }, 130);
    }
    var lv = audio.playing ? audio.level() : 0;
    state.energy = state.energy * 0.8 + lv * 0.2;
    var target = 0;
    if (audio.playing) target = Math.min(1, Math.pow(Math.max(0, lv) * MOUTH_GAIN, 0.75));
    else target = Math.max(0, Math.sin(t * 2) * 0.02);
    if (typeof window.FORCE_MOUTH === 'number') target = window.FORCE_MOUTH;
    var k = target > state.mouth ? 0.55 : 0.2;
    state.mouth += (target - state.mouth) * k;
    state.tilt = -state.mouth * 0.018 + state.energy * 0.14;
    drawPuppet();
    requestAnimationFrame(frame);
  }

  var Bridge = {
    es: null,
    onStart: function (text, dur) {
      this.showSubtitle(text, dur);
      this.setChip('播报中', 'ok');
    },
    onIdle: function () {
      this.setChip('已连接', 'ok');
      var el = document.getElementById('subtitle');
      setTimeout(function () { if (!audio.playing) el.classList.remove('show'); }, 600);
    },
    showSubtitle: function (text, dur) {
      var el = document.getElementById('subtitle');
      el.textContent = text;
      el.classList.add('show');
      clearTimeout(this._t);
      this._t = setTimeout(function () { el.classList.remove('show'); }, (dur + 1.2) * 1000);
    },
    pushDanmaku: function (user, text) {
      var box = document.getElementById('danmaku');
      if (!box) return;
      var el = document.createElement('div');
      el.className = 'dm';
      var b = document.createElement('b');
      b.textContent = (user || '观众') + '：';
      el.appendChild(b);
      el.appendChild(document.createTextNode(text || ''));
      box.appendChild(el);
      while (box.childNodes.length > 6) box.removeChild(box.firstChild);
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 22000);
    },
    setChip: function (txt, cls) {
      var chip = document.getElementById('chip');
      if (!chip) return;
      document.getElementById('chipText').textContent = txt;
      chip.className = cls || '';
    },
    connect: function () {
      var self = this;
      if (this.es) this.es.close();
      var es = new EventSource('/events');
      this.es = es;
      es.onopen = function () { self.setChip('已连接', 'ok'); };
      es.onerror = function () { self.setChip('连接断开，重试中…', 'bad'); };
      es.onmessage = function (ev) {
        var msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.type === 'hello') { self.setChip('已连接 · ' + (msg.voice || ''), 'ok'); return; }
        if (msg.type === 'say') { audio.resume().then(function () { audio.push(msg.url, msg.text); }); return; }
        if (msg.type === 'danmaku') { self.pushDanmaku(msg.user, msg.text); return; }
        if (msg.type === 'error') { self.setChip('服务端错误: ' + msg.message, 'bad'); }
      };
    }
  };
  window.PuppetBridge = Bridge;
  window.PuppetState = state;

  function boot() {
    var gate = document.getElementById('gate');
    if (gate) gate.classList.add('hide');
    audio.ensure();
    audio.resume();
    Bridge.connect();
  }
  var bs = document.getElementById('btnStart');
  if (bs) bs.addEventListener('click', boot);
  var bt = document.getElementById('btnTest');
  if (bt) bt.addEventListener('click', function () {
    audio.resume();
    fetch('/say', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: '大家好，我是 AI 主播，这是一条测试播报。' })
    }).catch(function (e) { Bridge.setChip('请求失败: ' + e.message, 'bad'); });
  });

  resize();
  requestAnimationFrame(frame);
  if (params.get('auto') === '1' || CLEAN) boot();
  setTimeout(function () { if (!Bridge.es) Bridge.connect(); }, 800);
})();
