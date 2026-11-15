/*
 * vk_fault_layer: Vulkan layer VK_LAYER_VMAFX_fault that injects deterministic
 * execution, submission, image view and fence failures for patch 0023 tests.
 *
 * VMAFX_VK_FAULT accepts a comma-separated list of at most 8 items (at most
 * 127 characters) from begin=N, submit=N, view=N (N in 1..1000) and fence.
 * The layer reads VMAFX_VK_FAULT through secure_getenv, as the Vulkan loader
 * reads its own variables, so a setuid or setgid process sees no fault
 * configuration. Build it with -D_GNU_SOURCE, which declares secure_getenv and
 * strnlen in glibc. On instance creation it writes "vk_fault: active
 * <value|none>" or "vk_fault: bad VMAFX_VK_FAULT value" to stderr; each
 * injected failure writes "vk_fault: injected <function> failure (call <N>)"
 * to stderr and returns VK_ERROR_OUT_OF_DEVICE_MEMORY.
 * tests/test_hqdn3d_vulkan_fault.py builds and loads this layer. The
 * loader-layer negotiation and link-chain interface follows
 * docs/LoaderLayerInterface.md of KhronosGroup/Vulkan-Loader (Apache-2.0).
 *
 * SPDX-FileCopyrightText: 2026 Lusoris
 * SPDX-License-Identifier: EUPL-1.2
 */

#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <vulkan/vulkan.h>
#include <vulkan/vk_layer.h>

enum {
    DECIMAL_BASE = 10,
    LAYER_INTERFACE_VERSION = 2,
    MIN_CALL = 1,
    MAX_CALL = 1000,
    MAX_FAULT_LEN = 127,
    FAULT_BUF_SIZE = 128,
    INT_MAX_DIGITS = 12,
    INT_BUF_SIZE = 16,
    MSG_BUF_SIZE = 192,
    MAX_FAULT_ITEMS = 8,
    MAX_CHAIN_STEPS = 32,
    MAX_MARKED_CMDS = 64,
    MAX_INSTANCES = 4,
    MAX_DEVICES = 4,
    MAX_SUBMITS = 16,
    MAX_CMD_INFOS = 16,
    INSTANCE_HOOK_COUNT = 4,
    DEVICE_HOOK_COUNT = 6,
};

struct dispatch_head {
    const void *key;
};

struct fault_config {
    int begin;
    int submit;
    int view;
    int fence;
};

struct instance_entry {
    const void *key;
    VkInstance instance;
    PFN_vkGetInstanceProcAddr next_gipa;
};

struct device_entry {
    const void *key;
    PFN_vkGetDeviceProcAddr next_gdpa;
    PFN_vkBeginCommandBuffer next_begin;
    PFN_vkCmdDispatch next_dispatch;
    PFN_vkQueueSubmit2 next_submit2;
    PFN_vkQueueSubmit2KHR next_submit2_khr;
    PFN_vkCreateImageView next_create_view;
    PFN_vkCreateFence next_create_fence;
};

struct fault_state {
    pthread_mutex_t mutex;
    struct fault_config cfg;
    int begin_calls;
    int submit_calls;
    int view_calls;
    int fence_calls;
    int injected;
    int dispatch_seen;
    VkCommandBuffer marked_cmds[MAX_MARKED_CMDS];
    int marked_count;
    struct instance_entry instances[MAX_INSTANCES];
    int instance_count;
    struct device_entry devices[MAX_DEVICES];
    int device_count;
};

/* Return the singleton layer state protected by its internal mutex. */
static struct fault_state *fault_state(void)
{
    static struct fault_state state = {
        .mutex = PTHREAD_MUTEX_INITIALIZER,
    };

    return &state;
}

/* Read the loader dispatch table pointer from a dispatchable handle. */
static const void *dispatch_key(const void *handle)
{
    const struct dispatch_head *head = (const struct dispatch_head *)handle;

    if (head == NULL) {
        return NULL;
    }
    return head->key;
}

/* Append src to dst[pos..MSG_BUF_SIZE-2], NUL-terminate and return new pos. */
static int append_str(char *dst, int pos, const char *src)
{
    int out = pos;

    if (out < 0 || out >= MSG_BUF_SIZE - 1) {
        return out;
    }
    for (int i = 0; out < MSG_BUF_SIZE - 1 && src[i] != '\0'; i++) {
        dst[out] = src[i];
        out++;
    }
    dst[out] = '\0';
    return out;
}

/* Format positive value as decimal ASCII into out[0..INT_BUF_SIZE-1]. */
static void format_positive_int(int value, char *out)
{
    char rev[INT_BUF_SIZE] = { 0 };
    int rem = value > 0 ? value : 0;
    int len = 0;

    for (int i = 0; i < INT_MAX_DIGITS; i++) {
        rev[len] = (char)('0' + (rem % DECIMAL_BASE));
        len++;
        rem /= DECIMAL_BASE;
        if (rem == 0) {
            break;
        }
    }
    for (int i = 0; i < INT_MAX_DIGITS; i++) {
        if (i >= len) {
            break;
        }
        out[i] = rev[len - 1 - i];
    }
    out[len] = '\0';
}

/* Write "vk_fault: active <label>\n" to stderr as a single buffered line. */
static void log_active(const char *label)
{
    char line[MSG_BUF_SIZE] = { 0 };
    int pos = 0;

    pos = append_str(line, pos, "vk_fault: active ");
    pos = append_str(line, pos, label);
    (void)append_str(line, pos, "\n");
    (void)fputs(line, stderr);
}

/* Write "vk_fault: injected <func_name> failure (call <call_num>)\n" to stderr. */
static void log_injected(const char *func_name, int call_num)
{
    char line[MSG_BUF_SIZE] = { 0 };
    char num[INT_BUF_SIZE] = { 0 };
    int pos = 0;

    format_positive_int(call_num, num);
    pos = append_str(line, pos, "vk_fault: injected ");
    pos = append_str(line, pos, func_name);
    pos = append_str(line, pos, " failure (call ");
    pos = append_str(line, pos, num);
    (void)append_str(line, pos, ")\n");
    (void)fputs(line, stderr);
}

/* Parse s as a whole decimal integer in MIN_CALL..MAX_CALL, or 0 on error. */
static int parse_count(const char *s)
{
    char *end = NULL;
    long val = 0;

    if (s == NULL || s[0] < '0' || s[0] > '9') {
        return 0;
    }
    errno = 0;
    val = strtol(s, &end, DECIMAL_BASE);
    if (errno != 0 || end == s || *end != '\0' || val < MIN_CALL || val > MAX_CALL) {
        return 0;
    }
    return (int)val;
}

/* Select the numeric configuration field named by key, or NULL when unknown. */
static int *config_slot(struct fault_config *cfg, const char *key)
{
    if (strcmp(key, "begin") == 0) {
        return &cfg->begin;
    }
    if (strcmp(key, "submit") == 0) {
        return &cfg->submit;
    }
    if (strcmp(key, "view") == 0) {
        return &cfg->view;
    }
    return NULL;
}

/* Parse one comma-delimited VMAFX_VK_FAULT item into cfg. */
static int parse_item(char *item, struct fault_config *cfg)
{
    char *eq = NULL;
    int *slot = NULL;
    int count = 0;

    if (strcmp(item, "fence") == 0) {
        if (cfg->fence != 0) {
            return -1;
        }
        cfg->fence = 1;
        return 0;
    }
    eq = strchr(item, '=');
    if (eq == NULL) {
        return -1;
    }
    *eq = '\0';
    slot = config_slot(cfg, item);
    count = parse_count(eq + 1);
    if (slot == NULL || *slot != 0 || count <= 0) {
        return -1;
    }
    *slot = count;
    return 0;
}

/* Parse the VMAFX_VK_FAULT environment string into cfg, returning 0 or -1. */
static int parse_fault_env(const char *raw, struct fault_config *cfg)
{
    char buf[FAULT_BUF_SIZE] = { 0 };
    char *cur = NULL;

    *cfg = (struct fault_config){ 0 };
    if (raw == NULL || raw[0] == '\0') {
        return 0;
    }
    if (strnlen(raw, MAX_FAULT_LEN + 1) > MAX_FAULT_LEN) {
        return -1;
    }
    for (int i = 0; i < FAULT_BUF_SIZE; i++) {
        buf[i] = raw[i];
        if (raw[i] == '\0') {
            break;
        }
    }
    cur = buf;
    for (int i = 0; i < MAX_FAULT_ITEMS; i++) {
        char *comma = strchr(cur, ',');
        if (comma != NULL) {
            *comma = '\0';
        }
        if (parse_item(cur, cfg) != 0) {
            return -1;
        }
        if (comma == NULL) {
            return 0;
        }
        cur = comma + 1;
    }
    return -1;
}

/* Return the stored downstream GetInstanceProcAddr for key, or NULL. */
static PFN_vkGetInstanceProcAddr find_instance_gipa(const struct fault_state *st,
                                                    const void *key)
{
    if (key == NULL) {
        return NULL;
    }
    for (int i = 0; i < MAX_INSTANCES; i++) {
        if (i >= st->instance_count) {
            break;
        }
        if (st->instances[i].key == key) {
            return st->instances[i].next_gipa;
        }
    }
    return NULL;
}

/* Return the stored device dispatch entry matching key, or NULL. */
static const struct device_entry *find_device(const struct fault_state *st, const void *key)
{
    if (key == NULL) {
        return NULL;
    }
    for (int i = 0; i < MAX_DEVICES; i++) {
        if (i >= st->device_count) {
            break;
        }
        if (st->devices[i].key == key) {
            return &st->devices[i];
        }
    }
    return NULL;
}

/* Return 1 when cmd has previously recorded a vkCmdDispatch call, else 0. */
static int is_marked_cmd_buf(const struct fault_state *st, VkCommandBuffer cmd)
{
    if (cmd == VK_NULL_HANDLE) {
        return 0;
    }
    for (int i = 0; i < MAX_MARKED_CMDS; i++) {
        if (i >= st->marked_count) {
            break;
        }
        if (st->marked_cmds[i] == cmd) {
            return 1;
        }
    }
    return 0;
}

/* Record cmd in marked_cmds when not already present and capacity remains. */
static void mark_cmd_buf(struct fault_state *st, VkCommandBuffer cmd)
{
    if (cmd == VK_NULL_HANDLE || is_marked_cmd_buf(st, cmd) != 0) {
        return;
    }
    if (st->marked_count < MAX_MARKED_CMDS) {
        st->marked_cmds[st->marked_count] = cmd;
        st->marked_count++;
    }
}

/* Return 1 when one VkSubmitInfo2 references a marked command buffer. */
static int submit_has_marked_cmd(const struct fault_state *st, const VkSubmitInfo2 *info)
{
    if (info->pCommandBufferInfos == NULL) {
        return 0;
    }
    for (int j = 0; j < MAX_CMD_INFOS; j++) {
        if ((uint32_t)j >= info->commandBufferInfoCount) {
            break;
        }
        if (is_marked_cmd_buf(st, info->pCommandBufferInfos[j].commandBuffer) != 0) {
            return 1;
        }
    }
    return 0;
}

/* Return 1 when a queue submission belongs to the filter or fence recovery. */
static int is_filter_submit(const struct fault_state *st, uint32_t submit_count,
                            const VkSubmitInfo2 *submits, VkFence fence)
{
    if (submit_count == 0) {
        return fence != VK_NULL_HANDLE ? 1 : 0;
    }
    if (submits == NULL) {
        return 0;
    }
    for (int i = 0; i < MAX_SUBMITS; i++) {
        if ((uint32_t)i >= submit_count) {
            break;
        }
        if (submit_has_marked_cmd(st, &submits[i]) != 0) {
            return 1;
        }
    }
    return 0;
}

/* Locate the loader link info in an instance pNext chain within 32 steps. */
static VkLayerInstanceCreateInfo *find_instance_link(const VkInstanceCreateInfo *create_info)
{
    const VkBaseInStructure *cur = (const VkBaseInStructure *)create_info->pNext;

    for (int i = 0; i < MAX_CHAIN_STEPS; i++) {
        if (cur == NULL) {
            return NULL;
        }
        if (cur->sType == VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO) {
            VkLayerInstanceCreateInfo *info = (VkLayerInstanceCreateInfo *)cur;
            if (info->function == VK_LAYER_LINK_INFO) {
                return info;
            }
        }
        cur = cur->pNext;
    }
    return NULL;
}

/* Locate the loader link info in a device pNext chain within 32 steps. */
static VkLayerDeviceCreateInfo *find_device_link(const VkDeviceCreateInfo *create_info)
{
    const VkBaseInStructure *cur = (const VkBaseInStructure *)create_info->pNext;

    for (int i = 0; i < MAX_CHAIN_STEPS; i++) {
        if (cur == NULL) {
            return NULL;
        }
        if (cur->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO) {
            VkLayerDeviceCreateInfo *info = (VkLayerDeviceCreateInfo *)cur;
            if (info->function == VK_LAYER_LINK_INFO) {
                return info;
            }
        }
        cur = cur->pNext;
    }
    return NULL;
}

/* Advance the instance link chain and resolve downstream vkCreateInstance. */
static PFN_vkCreateInstance resolve_create_instance(const VkInstanceCreateInfo *create_info,
                                                    PFN_vkGetInstanceProcAddr *out_gipa)
{
    VkLayerInstanceCreateInfo *chain = find_instance_link(create_info);
    PFN_vkGetInstanceProcAddr next_gipa = NULL;
    PFN_vkCreateInstance create_fn = NULL;

    if (chain == NULL || chain->u.pLayerInfo == NULL) {
        return NULL;
    }
    next_gipa = chain->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    if (next_gipa == NULL) {
        return NULL;
    }
    create_fn = (PFN_vkCreateInstance)next_gipa(VK_NULL_HANDLE, "vkCreateInstance");
    if (create_fn == NULL) {
        return NULL;
    }
    chain->u.pLayerInfo = chain->u.pLayerInfo->pNext;
    *out_gipa = next_gipa;
    return create_fn;
}

/* Record a created VkInstance and reset per-run fault state under the mutex. */
static VkResult store_instance(VkInstance instance, PFN_vkGetInstanceProcAddr next_gipa,
                               const struct fault_config *cfg)
{
    struct fault_state *st = fault_state();
    VkResult res = VK_SUCCESS;

    (void)pthread_mutex_lock(&st->mutex);
    if (st->instance_count >= MAX_INSTANCES) {
        res = VK_ERROR_INITIALIZATION_FAILED;
    } else {
        st->cfg = *cfg;
        st->begin_calls = 0;
        st->submit_calls = 0;
        st->view_calls = 0;
        st->fence_calls = 0;
        st->injected = 0;
        st->dispatch_seen = 0;
        st->marked_count = 0;
        st->instances[st->instance_count] = (struct instance_entry){
            .key = dispatch_key(instance),
            .instance = instance,
            .next_gipa = next_gipa,
        };
        st->instance_count++;
    }
    (void)pthread_mutex_unlock(&st->mutex);
    return res;
}

/* Parse VMAFX_VK_FAULT, advance the instance link chain and create an instance. */
static VkResult VKAPI_CALL fault_CreateInstance(const VkInstanceCreateInfo *pCreateInfo,
                                                const VkAllocationCallbacks *pAllocator,
                                                VkInstance *pInstance)
{
    const char *raw = secure_getenv("VMAFX_VK_FAULT");
    const char *label = "none";
    struct fault_config cfg = { 0 };
    struct fault_state *st = fault_state();
    int full = 0;
    PFN_vkGetInstanceProcAddr next_gipa = NULL;
    PFN_vkCreateInstance create_fn = NULL;
    VkResult res = VK_SUCCESS;

    if (parse_fault_env(raw, &cfg) != 0) {
        (void)fputs("vk_fault: bad VMAFX_VK_FAULT value\n", stderr);
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    if (raw != NULL && raw[0] != '\0') {
        label = raw;
    }
    log_active(label);
    if (pCreateInfo == NULL || pInstance == NULL) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    (void)pthread_mutex_lock(&st->mutex);
    full = st->instance_count >= MAX_INSTANCES;
    (void)pthread_mutex_unlock(&st->mutex);
    if (full != 0) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    create_fn = resolve_create_instance(pCreateInfo, &next_gipa);
    if (create_fn == NULL) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    res = create_fn(pCreateInfo, pAllocator, pInstance);
    if (res != VK_SUCCESS) {
        return res;
    }
    return store_instance(*pInstance, next_gipa, &cfg);
}

/* Advance the device link chain and resolve downstream vkCreateDevice. */
static PFN_vkCreateDevice resolve_create_device(VkInstance instance,
                                                const VkDeviceCreateInfo *create_info,
                                                PFN_vkGetDeviceProcAddr *out_gdpa)
{
    VkLayerDeviceCreateInfo *chain = find_device_link(create_info);
    PFN_vkGetInstanceProcAddr next_gipa = NULL;
    PFN_vkGetDeviceProcAddr next_gdpa = NULL;
    PFN_vkCreateDevice create_fn = NULL;

    if (chain == NULL || chain->u.pLayerInfo == NULL) {
        return NULL;
    }
    next_gipa = chain->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    next_gdpa = chain->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    if (next_gipa == NULL || next_gdpa == NULL) {
        return NULL;
    }
    create_fn = (PFN_vkCreateDevice)next_gipa(instance, "vkCreateDevice");
    if (create_fn == NULL) {
        return NULL;
    }
    chain->u.pLayerInfo = chain->u.pLayerInfo->pNext;
    *out_gdpa = next_gdpa;
    return create_fn;
}

/* Populate downstream device function pointers for a newly created device. */
static void init_device_entry(struct device_entry *entry, VkDevice device,
                              PFN_vkGetDeviceProcAddr next_gdpa)
{
    entry->key = dispatch_key(device);
    entry->next_gdpa = next_gdpa;
    entry->next_begin = (PFN_vkBeginCommandBuffer)next_gdpa(device, "vkBeginCommandBuffer");
    entry->next_dispatch = (PFN_vkCmdDispatch)next_gdpa(device, "vkCmdDispatch");
    entry->next_submit2 = (PFN_vkQueueSubmit2)next_gdpa(device, "vkQueueSubmit2");
    entry->next_submit2_khr = (PFN_vkQueueSubmit2KHR)next_gdpa(device, "vkQueueSubmit2KHR");
    entry->next_create_view = (PFN_vkCreateImageView)next_gdpa(device, "vkCreateImageView");
    entry->next_create_fence = (PFN_vkCreateFence)next_gdpa(device, "vkCreateFence");
}

/* Advance the device link chain, create a device and store its dispatch table. */
static VkResult VKAPI_CALL fault_CreateDevice(VkPhysicalDevice physicalDevice,
                                              const VkDeviceCreateInfo *pCreateInfo,
                                              const VkAllocationCallbacks *pAllocator,
                                              VkDevice *pDevice)
{
    struct fault_state *st = fault_state();
    VkInstance instance = VK_NULL_HANDLE;
    int ready = 0;
    PFN_vkGetDeviceProcAddr next_gdpa = NULL;
    PFN_vkCreateDevice create_fn = NULL;
    struct device_entry entry = { 0 };
    VkResult res = VK_SUCCESS;

    if (physicalDevice == VK_NULL_HANDLE || pCreateInfo == NULL || pDevice == NULL) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    (void)pthread_mutex_lock(&st->mutex);
    if (st->instance_count > 0 && st->device_count < MAX_DEVICES) {
        instance = st->instances[st->instance_count - 1].instance;
        ready = 1;
    }
    (void)pthread_mutex_unlock(&st->mutex);
    if (ready == 0) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    create_fn = resolve_create_device(instance, pCreateInfo, &next_gdpa);
    if (create_fn == NULL) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    res = create_fn(physicalDevice, pCreateInfo, pAllocator, pDevice);
    if (res != VK_SUCCESS) {
        return res;
    }
    init_device_entry(&entry, *pDevice, next_gdpa);
    (void)pthread_mutex_lock(&st->mutex);
    if (st->device_count >= MAX_DEVICES) {
        res = VK_ERROR_INITIALIZATION_FAILED;
    } else {
        st->devices[st->device_count] = entry;
        st->device_count++;
    }
    (void)pthread_mutex_unlock(&st->mutex);
    return res;
}

/* Mark commandBuffer as a compute command buffer and forward vkCmdDispatch. */
static void VKAPI_CALL fault_CmdDispatch(VkCommandBuffer commandBuffer, uint32_t groupCountX,
                                         uint32_t groupCountY, uint32_t groupCountZ)
{
    struct fault_state *st = fault_state();
    const struct device_entry *dev = NULL;
    PFN_vkCmdDispatch next_fn = NULL;

    (void)pthread_mutex_lock(&st->mutex);
    mark_cmd_buf(st, commandBuffer);
    st->dispatch_seen = 1;
    dev = find_device(st, dispatch_key(commandBuffer));
    if (dev != NULL) {
        next_fn = dev->next_dispatch;
    }
    (void)pthread_mutex_unlock(&st->mutex);
    if (next_fn != NULL) {
        next_fn(commandBuffer, groupCountX, groupCountY, groupCountZ);
    }
}

/* Count vkBeginCommandBuffer on marked command buffers and inject at begin. */
static VkResult VKAPI_CALL fault_BeginCommandBuffer(VkCommandBuffer commandBuffer,
                                                    const VkCommandBufferBeginInfo *pBeginInfo)
{
    struct fault_state *st = fault_state();
    const struct device_entry *dev = NULL;
    PFN_vkBeginCommandBuffer next_fn = NULL;
    int inject = 0;
    int call_num = 0;

    (void)pthread_mutex_lock(&st->mutex);
    dev = find_device(st, dispatch_key(commandBuffer));
    if (dev != NULL) {
        next_fn = dev->next_begin;
    }
    if (is_marked_cmd_buf(st, commandBuffer) != 0) {
        st->begin_calls++;
        if (st->cfg.begin > 0 && st->begin_calls == st->cfg.begin) {
            inject = 1;
            call_num = st->begin_calls;
            st->injected = 1;
        }
    }
    (void)pthread_mutex_unlock(&st->mutex);
    if (inject != 0) {
        log_injected("vkBeginCommandBuffer", call_num);
        return VK_ERROR_OUT_OF_DEVICE_MEMORY;
    }
    if (next_fn == NULL) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    return next_fn(commandBuffer, pBeginInfo);
}

/* Count filter submissions and inject from the configured submit call onward. */
static VkResult handle_queue_submit2(VkQueue queue, uint32_t submit_count,
                                     const VkSubmitInfo2 *submits, VkFence fence, int is_khr)
{
    struct fault_state *st = fault_state();
    const struct device_entry *dev = NULL;
    PFN_vkQueueSubmit2 next_fn = NULL;
    const char *name = is_khr != 0 ? "vkQueueSubmit2KHR" : "vkQueueSubmit2";
    int inject = 0;
    int call_num = 0;

    (void)pthread_mutex_lock(&st->mutex);
    dev = find_device(st, dispatch_key(queue));
    if (dev != NULL) {
        next_fn = is_khr != 0 ? dev->next_submit2_khr : dev->next_submit2;
    }
    if (is_filter_submit(st, submit_count, submits, fence) != 0) {
        st->submit_calls++;
        if (st->cfg.submit > 0 && st->submit_calls >= st->cfg.submit) {
            inject = 1;
            call_num = st->submit_calls;
            st->injected = 1;
        }
    }
    (void)pthread_mutex_unlock(&st->mutex);
    if (inject != 0) {
        log_injected(name, call_num);
        return VK_ERROR_OUT_OF_DEVICE_MEMORY;
    }
    if (next_fn == NULL) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    return next_fn(queue, submit_count, submits, fence);
}

/* Intercept core vkQueueSubmit2 through the shared submission fault helper. */
static VkResult VKAPI_CALL fault_QueueSubmit2(VkQueue queue, uint32_t submitCount,
                                              const VkSubmitInfo2 *pSubmits, VkFence fence)
{
    return handle_queue_submit2(queue, submitCount, pSubmits, fence, 0);
}

/* Intercept extension vkQueueSubmit2KHR through the shared submission helper. */
static VkResult VKAPI_CALL fault_QueueSubmit2KHR(VkQueue queue, uint32_t submitCount,
                                                 const VkSubmitInfo2 *pSubmits, VkFence fence)
{
    return handle_queue_submit2(queue, submitCount, pSubmits, fence, 1);
}

/* Count vkCreateImageView calls after the first dispatch and inject at view. */
static VkResult VKAPI_CALL fault_CreateImageView(VkDevice device,
                                                 const VkImageViewCreateInfo *pCreateInfo,
                                                 const VkAllocationCallbacks *pAllocator,
                                                 VkImageView *pView)
{
    struct fault_state *st = fault_state();
    const struct device_entry *dev = NULL;
    PFN_vkCreateImageView next_fn = NULL;
    int inject = 0;
    int call_num = 0;

    (void)pthread_mutex_lock(&st->mutex);
    dev = find_device(st, dispatch_key(device));
    if (dev != NULL) {
        next_fn = dev->next_create_view;
    }
    if (st->dispatch_seen != 0) {
        st->view_calls++;
        if (st->cfg.view > 0 && st->view_calls == st->cfg.view) {
            inject = 1;
            call_num = st->view_calls;
            st->injected = 1;
        }
    }
    (void)pthread_mutex_unlock(&st->mutex);
    if (inject != 0) {
        log_injected("vkCreateImageView", call_num);
        return VK_ERROR_OUT_OF_DEVICE_MEMORY;
    }
    if (next_fn == NULL) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    return next_fn(device, pCreateInfo, pAllocator, pView);
}

/* Inject vkCreateFence failures on every call once a fault has fired. */
static VkResult VKAPI_CALL fault_CreateFence(VkDevice device,
                                             const VkFenceCreateInfo *pCreateInfo,
                                             const VkAllocationCallbacks *pAllocator,
                                             VkFence *pFence)
{
    struct fault_state *st = fault_state();
    const struct device_entry *dev = NULL;
    PFN_vkCreateFence next_fn = NULL;
    int inject = 0;
    int call_num = 0;

    (void)pthread_mutex_lock(&st->mutex);
    dev = find_device(st, dispatch_key(device));
    if (dev != NULL) {
        next_fn = dev->next_create_fence;
    }
    if (st->injected != 0) {
        st->fence_calls++;
        if (st->cfg.fence != 0) {
            inject = 1;
            call_num = st->fence_calls;
        }
    }
    (void)pthread_mutex_unlock(&st->mutex);
    if (inject != 0) {
        log_injected("vkCreateFence", call_num);
        return VK_ERROR_OUT_OF_DEVICE_MEMORY;
    }
    if (next_fn == NULL) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    return next_fn(device, pCreateInfo, pAllocator, pFence);
}

/* Return intercepted device hooks when supported downstream, else chain down. */
static PFN_vkVoidFunction VKAPI_CALL fault_GetDeviceProcAddr(VkDevice device, const char *pName)
{
    struct device_hook {
        const char *name;
        PFN_vkVoidFunction hook;
        PFN_vkVoidFunction next;
    };
    struct fault_state *st = fault_state();
    const struct device_entry *found = NULL;
    struct device_entry dev = { 0 };
    int known = 0;

    if (pName == NULL) {
        return NULL;
    }
    if (strcmp(pName, "vkGetDeviceProcAddr") == 0) {
        return (PFN_vkVoidFunction)fault_GetDeviceProcAddr;
    }
    (void)pthread_mutex_lock(&st->mutex);
    found = find_device(st, dispatch_key(device));
    if (found != NULL) {
        dev = *found;
        known = 1;
    }
    (void)pthread_mutex_unlock(&st->mutex);
    if (known == 0) {
        return NULL;
    }
    {
        const struct device_hook hooks[DEVICE_HOOK_COUNT] = {
            { "vkBeginCommandBuffer", (PFN_vkVoidFunction)fault_BeginCommandBuffer,
              (PFN_vkVoidFunction)dev.next_begin, },
            { "vkCmdDispatch", (PFN_vkVoidFunction)fault_CmdDispatch,
              (PFN_vkVoidFunction)dev.next_dispatch, },
            { "vkQueueSubmit2", (PFN_vkVoidFunction)fault_QueueSubmit2,
              (PFN_vkVoidFunction)dev.next_submit2, },
            { "vkQueueSubmit2KHR", (PFN_vkVoidFunction)fault_QueueSubmit2KHR,
              (PFN_vkVoidFunction)dev.next_submit2_khr, },
            { "vkCreateImageView", (PFN_vkVoidFunction)fault_CreateImageView,
              (PFN_vkVoidFunction)dev.next_create_view, },
            { "vkCreateFence", (PFN_vkVoidFunction)fault_CreateFence,
              (PFN_vkVoidFunction)dev.next_create_fence, },
        };
        for (int i = 0; i < DEVICE_HOOK_COUNT; i++) {
            if (strcmp(pName, hooks[i].name) == 0 && hooks[i].next != NULL) {
                return hooks[i].hook;
            }
        }
    }
    return dev.next_gdpa(device, pName);
}

/* Return intercepted instance hooks or forward to the stored next resolver. */
static PFN_vkVoidFunction VKAPI_CALL fault_GetInstanceProcAddr(VkInstance instance,
                                                               const char *pName)
{
    struct instance_hook {
        const char *name;
        PFN_vkVoidFunction hook;
    };
    const struct instance_hook hooks[INSTANCE_HOOK_COUNT] = {
        { "vkGetInstanceProcAddr", (PFN_vkVoidFunction)fault_GetInstanceProcAddr },
        { "vkCreateInstance", (PFN_vkVoidFunction)fault_CreateInstance },
        { "vkCreateDevice", (PFN_vkVoidFunction)fault_CreateDevice },
        { "vkGetDeviceProcAddr", (PFN_vkVoidFunction)fault_GetDeviceProcAddr },
    };
    struct fault_state *st = fault_state();
    PFN_vkGetInstanceProcAddr next_gipa = NULL;

    if (pName == NULL) {
        return NULL;
    }
    for (int i = 0; i < INSTANCE_HOOK_COUNT; i++) {
        if (strcmp(pName, hooks[i].name) == 0) {
            return hooks[i].hook;
        }
    }
    (void)pthread_mutex_lock(&st->mutex);
    next_gipa = find_instance_gipa(st, dispatch_key(instance));
    (void)pthread_mutex_unlock(&st->mutex);
    if (next_gipa == NULL) {
        return NULL;
    }
    return next_gipa(instance, pName);
}

/* Negotiate loader-layer interface version 2 and export layer proc resolvers. */
VKAPI_ATTR VkResult VKAPI_CALL vkNegotiateLoaderLayerInterfaceVersion(
    VkNegotiateLayerInterface *pVersionStruct)
{
    if (pVersionStruct == NULL || pVersionStruct->sType != LAYER_NEGOTIATE_INTERFACE_STRUCT) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    pVersionStruct->pfnGetInstanceProcAddr = fault_GetInstanceProcAddr;
    pVersionStruct->pfnGetDeviceProcAddr = fault_GetDeviceProcAddr;
    pVersionStruct->pfnGetPhysicalDeviceProcAddr = NULL;
    if (pVersionStruct->loaderLayerInterfaceVersion > (uint32_t)LAYER_INTERFACE_VERSION) {
        pVersionStruct->loaderLayerInterfaceVersion = (uint32_t)LAYER_INTERFACE_VERSION;
    }
    return VK_SUCCESS;
}
