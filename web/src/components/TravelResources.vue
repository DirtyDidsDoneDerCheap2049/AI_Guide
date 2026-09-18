<script setup lang="ts">
import type { TravelResource } from '../api/types'
defineProps<{ resources: TravelResource[] }>()
</script>
<template>
  <div class="resource-links">
    <a
      v-for="item in resources"
      :key="item.url"
      :href="item.url"
      target="_blank"
      rel="noopener noreferrer"
      class="resource-link"
    >
      <span class="resource-kind">{{
        item.is_search ? '搜索' : item.kind === 'video' ? '▷ 视频' : '攻略'
      }}</span>
      <span
        ><strong>{{ item.title }}</strong
        ><small
          >{{ item.source }}<template v-if="item.published_at"> · {{ item.published_at }}</template
          ><template v-else-if="!item.is_search"> · 发布时间未标注</template></small
        ></span
      ><span>↗</span>
    </a>
  </div>
</template>
