#include <linux/mm.h>
#include <linux/kallsyms.h>
#include <linux/kprobes.h>

typedef int (*shmem_zero_setup_ptr_t)(struct vm_area_struct *);
static shmem_zero_setup_ptr_t shmem_zero_setup_ptr = NULL;

/*
 * kallsyms_lookup_name() is no longer exported to modules (since 5.7).
 * Resolve the internal symbol by registering a kprobe on it by name and
 * reading back the address the kernel resolved for us, then unregister.
 */
static unsigned long lookup_symbol_addr(const char *name)
{
	struct kprobe kp = { .symbol_name = name };
	unsigned long addr = 0;

	if (register_kprobe(&kp) == 0) {
		addr = (unsigned long) kp.addr;
		unregister_kprobe(&kp);
	}

	return addr;
}

int shmem_zero_setup(struct vm_area_struct *vma)
{
	if (!shmem_zero_setup_ptr)
		shmem_zero_setup_ptr = (shmem_zero_setup_ptr_t)
			lookup_symbol_addr("shmem_zero_setup");

	if (!shmem_zero_setup_ptr)
		return -ENOSYS;

	return shmem_zero_setup_ptr(vma);
}
