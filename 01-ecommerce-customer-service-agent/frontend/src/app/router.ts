import { createRouter, createWebHistory } from 'vue-router'
import { useAuth } from '../stores/auth'
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: () => import('../pages/CatalogPage.vue') },
    { path: '/login', component: () => import('../pages/LoginPage.vue') },
    { path: '/cart', component: () => import('../pages/CartPage.vue'), meta: { role: 'customer' } },
    {
      path: '/checkout',
      component: () => import('../pages/CheckoutPage.vue'),
      meta: { role: 'customer' },
    },
    {
      path: '/wallet',
      component: () => import('../pages/WalletPage.vue'),
      meta: { role: 'customer' },
    },
    {
      path: '/orders/:id?',
      component: () => import('../pages/OrdersPage.vue'),
      meta: { role: 'customer' },
    },
    {
      path: '/shop',
      component: () => import('../pages/ShopPage.vue'),
      meta: { role: 'shop_admin' },
    },
    {
      path: '/knowledge',
      component: () => import('../features/knowledge/KnowledgePage.vue'),
      meta: { role: 'knowledge_admin' },
    },
    {
      path: '/workbench',
      component: () => import('../features/support/SupportPage.vue'),
      meta: { roles: ['support', 'supervisor'] },
    },
    {
      path: '/experience',
      component: () => import('../features/support/ExperiencePage.vue'),
      meta: { role: 'supervisor' },
    },
    {
      path: '/service',
      component: () => import('../features/support/ServicePage.vue'),
      meta: { roles: ['customer', 'support', 'supervisor', 'shop_admin'] },
    },
    { path: '/:pathMatch(.*)*', component: () => import('../pages/NotFound.vue') },
  ],
  scrollBehavior: () => ({ top: 0 }),
})
router.beforeEach(async (to) => {
  const auth = useAuth()
  try {
    await auth.restore()
  } catch {
    return to.path === '/login' ? true : { path: '/login', query: { unavailable: '1' } }
  }
  if ((to.meta.role || to.meta.roles) && !auth.session)
    return { path: '/login', query: { next: to.fullPath } }
  if (Array.isArray(to.meta.roles) && !to.meta.roles.includes(auth.session?.active_role))
    return { path: '/login', query: { forbidden: '1' } }
  if (to.meta.role && auth.session?.active_role !== to.meta.role)
    return { path: '/login', query: { forbidden: '1' } }
})
