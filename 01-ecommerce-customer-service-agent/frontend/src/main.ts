import { createApp } from 'vue'
import { createPinia } from 'pinia'
import 'element-plus/es/components/dialog/style/css'
import 'element-plus/es/components/table/style/css'
import './style.css'
import App from './app/App.vue'
import { router } from './app/router'

createApp(App).use(createPinia()).use(router).mount('#app')
