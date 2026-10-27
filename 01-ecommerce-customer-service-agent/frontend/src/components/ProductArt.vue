<script setup lang="ts">
defineProps<{ kind: string; name?: string }>()
</script>
<template>
  <svg
    viewBox="0 0 420 320"
    role="img"
    :aria-label="name || '商品插画'"
    xmlns="http://www.w3.org/2000/svg"
  >
    <defs>
      <linearGradient id="ceramic" x1="0" y1="0" x2="1" y2="1">
        <stop stop-color="#fffdf5" />
        <stop offset="1" stop-color="#d8c9b5" />
      </linearGradient>
      <linearGradient id="orange" x1="0" y1="0" x2="1" y2="1">
        <stop stop-color="#f1ac80" />
        <stop offset="1" stop-color="#be613c" />
      </linearGradient>
    </defs>
    <ellipse cx="211" cy="267" rx="107" ry="16" fill="#544137" opacity=".1" />
    <g v-if="kind === 'lamp'">
      <path d="M213 115v137" stroke="#c88356" stroke-width="11" />
      <ellipse cx="213" cy="258" rx="69" ry="13" fill="#d49b70" />
      <path d="M129 143L163 63h99l36 80z" fill="#f3d7a5" />
      <ellipse cx="213" cy="143" rx="85" ry="12" fill="#d4a96f" />
      <ellipse cx="213" cy="144" rx="64" ry="6" fill="#ffe9b9" />
    </g>
    <g v-else-if="kind === 'bag'">
      <path d="M153 113V87c0-68 117-68 117 0v26" fill="none" stroke="#a98157" stroke-width="17" />
      <path d="M119 111h188l20 144H100z" fill="#dfcaa7" />
      <path d="M142 118l-10 125M286 118l9 125" stroke="#bea27b" stroke-width="2" />
      <path d="M185 174h55v42h-55z" fill="#c97040" />
      <text x="199" y="202" fill="#fff4df" font-size="24">o</text>
    </g>
    <g v-else-if="kind === 'vase'">
      <path
        d="M196 124Q170 58 238 22M207 120Q245 65 291 63"
        fill="none"
        stroke="#748061"
        stroke-width="4"
      />
      <ellipse cx="227" cy="49" rx="11" ry="23" fill="#9ba583" transform="rotate(35 227 49)" />
      <ellipse cx="261" cy="69" rx="25" ry="10" fill="#788b6d" transform="rotate(-15 261 69)" />
      <path
        d="M180 115h66v37c1 28 30 39 26 70-3 24-22 40-58 40s-58-16-58-40c0-31 24-42 24-70z"
        fill="url(#orange)"
      />
      <ellipse cx="213" cy="115" rx="33" ry="7" fill="#8f573a" />
    </g>
    <g v-else>
      <path d="M277 125c72-14 80 100 8 105" fill="none" stroke="#d2bea6" stroke-width="22" />
      <path d="M122 107h170l-15 119q-4 34-70 34t-70-34z" fill="url(#ceramic)" />
      <ellipse cx="207" cy="108" rx="85" ry="19" fill="#faf2e5" />
      <ellipse cx="207" cy="110" rx="69" ry="12" fill="#b19b80" />
      <path
        d="M158 86c-21-31 22-29 0-59M201 78c-20-27 18-28 4-50"
        fill="none"
        stroke="#b29b83"
        stroke-width="4"
        opacity=".35"
        stroke-linecap="round"
      />
    </g>
  </svg>
</template>
