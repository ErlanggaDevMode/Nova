// Nova 3D avatar: renders nova_avatar.glb and reacts to `nova:state` events.
//
// window.dispatchEvent(new CustomEvent('nova:state', { detail: { state, text } }))
// states: idle | listening | thinking | talking | error
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const MODEL_URL = '/static/avatar/nova_avatar.glb';
const STATES = ['idle', 'listening', 'thinking', 'talking', 'error'];
const ERROR_HOLD_MS = 2200;

// Per-state targets: morph weights and head pose (radians: pitch up +, yaw left +, roll)
const POSES = {
    idle:      { smile: 0.35, mouth_open: 0, confused: 0, pitch: 0.00, yaw: 0.0, roll: 0.00 },
    listening: { smile: 0.15, mouth_open: 0, confused: 0, pitch: -0.05, yaw: 0.0, roll: 0.14 },
    thinking:  { smile: 0.0,  mouth_open: 0, confused: 0, pitch: 0.16, yaw: 0.18, roll: -0.06 },
    talking:   { smile: 0.45, mouth_open: 0, confused: 0, pitch: 0.02, yaw: 0.0, roll: 0.03 },
    error:     { smile: 0.0,  mouth_open: 0, confused: 1, pitch: -0.04, yaw: -0.1, roll: -0.16 },
};

export function talkDuration(text) {
    const len = (text || '').length;
    return Math.min(6000, Math.max(1200, 400 + 45 * len));
}

const ctx = {
    el: null,
    state: 'idle',
    timer: null,
    talkUntil: 0,
};

export function setState(state, opts = {}) {
    if (!STATES.includes(state)) return;
    clearTimeout(ctx.timer);
    ctx.state = state;
    if (ctx.el) ctx.el.dataset.state = state;
    if (state === 'talking') {
        const ms = talkDuration(opts.text);
        ctx.talkUntil = performance.now() + ms;
        ctx.timer = setTimeout(() => setState('idle'), ms);
    } else if (state === 'error') {
        ctx.timer = setTimeout(() => setState('idle'), ERROR_HOLD_MS);
    }
}

function fallback(el, err) {
    console.warn('Nova avatar unavailable, showing fallback:', err);
    el.innerHTML = '<div class="avatar-fallback"><span class="logo-icon">▲</span> Nova</div>';
    el.dataset.avatar = 'fallback';
}

// Rotate a bone by a world-space delta on top of its current world rotation.
function rotateBoneWorld(bone, worldDelta) {
    const boneWorld = bone.getWorldQuaternion(new THREE.Quaternion());
    const parentWorld = bone.parent.getWorldQuaternion(new THREE.Quaternion());
    bone.quaternion.copy(parentWorld.invert().multiply(worldDelta.multiply(boneWorld)));
    bone.updateMatrixWorld(true);
}

// Set bone to its base local rotation, then rotate it by a world-space delta.
const _pw = new THREE.Quaternion();
const _pwi = new THREE.Quaternion();
function applyWorldOffset(bone, baseLocal, worldDelta) {
    bone.parent.updateWorldMatrix(true, false);
    bone.parent.getWorldQuaternion(_pw);
    _pwi.copy(_pw).invert();
    bone.quaternion.copy(_pwi).multiply(worldDelta).multiply(_pw).multiply(baseLocal);
}

export async function mount(el) {
    ctx.el = el;
    el.dataset.state = ctx.state;

    let renderer;
    try {
        renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch (err) {
        fallback(el, err);
        return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    el.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(26, 1, 0.01, 10);
    camera.position.set(0, 0.3, 1.25);
    camera.lookAt(0, 0.255, 0);

    scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x1a2030, 1.6));
    const key = new THREE.DirectionalLight(0xffffff, 2.2);
    key.position.set(-0.6, 0.9, 0.9);
    scene.add(key);
    const rim = new THREE.DirectionalLight(0x2e8fff, 2.5);
    rim.position.set(0.4, 0.8, -0.9);
    scene.add(rim);

    let gltf;
    try {
        gltf = await new GLTFLoader().loadAsync(MODEL_URL);
    } catch (err) {
        renderer.dispose();
        fallback(el, err);
        return;
    }
    const model = gltf.scene;
    scene.add(model);
    model.updateMatrixWorld(true);

    const bones = {};
    model.traverse((o) => { if (o.isBone) bones[o.name] = o; });
    const morphMeshes = [];
    model.traverse((o) => { if (o.morphTargetDictionary) morphMeshes.push(o); });

    // Relaxed pose: arms down from the T-pose
    const down = THREE.MathUtils.degToRad(65);
    if (bones.RightArm) rotateBoneWorld(bones.RightArm, new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), down));
    if (bones.Left_Arm) rotateBoneWorld(bones.Left_Arm, new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), -down));

    const base = {};
    for (const name of ['Head', 'Neck', 'Spine1', 'Spine2']) {
        if (bones[name]) base[name] = bones[name].quaternion.clone();
    }

    const cur = { smile: 0, mouth_open: 0, confused: 0, blink: 0, pitch: 0, yaw: 0, roll: 0 };
    let nextBlink = performance.now() + 1500;
    let blinkStart = -1;
    let glanceYaw = 0;
    let nextGlance = performance.now() + 4000;

    function setMorph(name, value) {
        for (const m of morphMeshes) {
            const i = m.morphTargetDictionary[name];
            if (i !== undefined) m.morphTargetInfluences[i] = value;
        }
    }

    function resize() {
        const w = el.clientWidth;
        const h = el.clientHeight;
        if (!w || !h) return;
        renderer.setSize(w, h, false);
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
    }
    new ResizeObserver(resize).observe(el);
    resize();

    const euler = new THREE.Euler();
    const qOff = new THREE.Quaternion();
    let last = performance.now();

    function frame(now) {
        requestAnimationFrame(frame);
        if (document.hidden) { last = now; return; }
        const dt = Math.min(0.1, (now - last) / 1000);
        last = now;
        const t = now / 1000;
        const target = POSES[ctx.state];
        const k = Math.min(1, dt * 8);

        // Idle glances
        if (now > nextGlance) {
            glanceYaw = ctx.state === 'idle' ? (Math.random() - 0.5) * 0.35 : 0;
            nextGlance = now + 2500 + Math.random() * 4000;
        }

        for (const key of ['smile', 'confused', 'pitch', 'roll']) cur[key] += (target[key] - cur[key]) * k;
        cur.yaw += (target.yaw + glanceYaw - cur.yaw) * k * 0.6;

        // Talking: mouth flaps while the reply is "spoken"
        const talking = ctx.state === 'talking' && now < ctx.talkUntil;
        const flap = talking ? 0.25 + 0.75 * Math.abs(Math.sin(t * 11) * Math.sin(t * 4.3 + 1)) : 0;
        cur.mouth_open += (flap - cur.mouth_open) * Math.min(1, dt * 20);

        // Blinks every 2–6 s (a quick 160 ms close/open)
        if (blinkStart < 0 && now > nextBlink) blinkStart = now;
        if (blinkStart >= 0) {
            const p = (now - blinkStart) / 160;
            cur.blink = p < 1 ? Math.sin(Math.PI * p) : 0;
            if (p >= 1) { blinkStart = -1; nextBlink = now + 2000 + Math.random() * 4000; }
        }

        setMorph('smile', cur.smile * (1 - cur.mouth_open * 0.6));
        setMorph('confused', cur.confused);
        setMorph('mouth_open', cur.mouth_open);
        setMorph('blink', cur.blink);

        // Head pose + breathing
        // Offsets are world-space (x: nod, y: turn, z: tilt) because the rig's bone axes are arbitrary.
        const breath = Math.sin(t * 1.7) * 0.012;
        if (bones.Spine2) {
            euler.set(breath, 0, Math.sin(t * 0.8) * 0.015);
            applyWorldOffset(bones.Spine2, base.Spine2, qOff.setFromEuler(euler));
        }
        if (bones.Head) {
            euler.set(cur.pitch, cur.yaw, cur.roll + Math.sin(t * 0.9) * 0.02);
            applyWorldOffset(bones.Head, base.Head, qOff.setFromEuler(euler));
        }
        model.position.y = Math.max(0, Math.sin(t * 1.7)) * 0.002;

        renderer.render(scene, camera);
    }
    requestAnimationFrame(frame);
    el.dataset.avatar = 'ready';
}

window.addEventListener('nova:state', (e) => setState(e.detail?.state, e.detail || {}));
window.NovaAvatar = { setState, talkDuration, get state() { return ctx.state; } };

const mountEl = document.getElementById('nova-avatar');
if (mountEl) mount(mountEl).catch((err) => fallback(mountEl, err));
