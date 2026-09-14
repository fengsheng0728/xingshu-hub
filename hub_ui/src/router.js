import { createRouter, createWebHashHistory } from 'vue-router'

// 信息架构（§1.2：4 区 10+1 页，角色任务视角）
// group 用于侧栏分组展示
export const NAV = [
  {
    group: '概览',
    items: [
      { path: '/overview', name: 'overview', title: '总览 Overview' },
      { path: '/activity', name: 'activity', title: '活动 Activity' },
    ],
  },
  {
    group: '治理',
    items: [
      { path: '/access', name: 'access', title: '访问与权限 Access' },
      { path: '/disclosure', name: 'disclosure', title: '披露治理 Disclosure' },
      { path: '/audit', name: 'audit', title: '审计中心 Audit' },
    ],
  },
  {
    group: '数据',
    items: [
      { path: '/knowledge', name: 'knowledge', title: '知识 Knowledge' },
      { path: '/wiki', name: 'wiki', title: 'Wiki' },
      { path: '/memory', name: 'memory', title: '记忆 Memory' },
    ],
  },
  {
    group: '系统',
    items: [
      { path: '/ops', name: 'ops', title: '运行 Ops' },
      { path: '/integrations', name: 'integrations', title: '集成 Integrations' },
      { path: '/settings', name: 'settings', title: '设置 Settings' },
    ],
  },
]

const routes = [
  { path: '/', redirect: '/overview' },
  ...NAV.flatMap((g) =>
    g.items.map((it) => ({
      ...it,
      component: () => import(`./views/${it.name[0].toUpperCase()}${it.name.slice(1)}View.vue`),
      meta: { title: it.title, group: g.group },
    }))
  ),
]

// hash 路由：静态托管无服务端 rewrite，深链可直达
export const router = createRouter({
  history: createWebHashHistory(),
  routes,
})
