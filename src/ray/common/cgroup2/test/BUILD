load("//bazel:ray.bzl", "ray_cc_library", "ray_cc_test")

ray_cc_library(
    name = "cgroup_test_utils",
    srcs = ["cgroup_test_utils.cc"],
    hdrs = ["cgroup_test_utils.h"],
    deps = [
        "//src/ray/common:status",
        "//src/ray/common:status_or",
        "@com_google_absl//absl/strings:str_format",
    ],
)

ray_cc_test(
    name = "sysfs_cgroup_driver_test",
    srcs = ["sysfs_cgroup_driver_test.cc"],
    tags = [
        "team:core",
    ],
    deps = [
        ":cgroup_test_utils",
        "//src/ray/common:status",
        "//src/ray/common:status_or",
        "//src/ray/common/cgroup2:sysfs_cgroup_driver",
        "//src/ray/common/test:testing",
        "@com_google_absl//absl/strings:str_format",
        "@com_google_googletest//:gtest_main",
    ],
)
