#ifndef __JDK_CAMERA_H__
#define __JDK_CAMERA_H__

#include <linux/videodev2.h>

#include <memory>
#include <string>
#include <vector>

#include "JdkFrame.hpp"
// #include "data_type.hpp"
// #include "jdk_log.h"

class JdkCamera {
public:
	static std::shared_ptr<JdkCamera> create(const std::string& device,
											 int				width,
											 int				height,
											 __u32				pixfmt,
											 int				req_count = 4);
	// get a frame of data
	JdkFramePtr getFrame();

	~JdkCamera();

private:
	JdkCamera(const std::string& device);
	// the internal data structure hides the implementation details
	class Impl;
	std::unique_ptr<Impl> impl_;
};
using JdkCameraPtr = std::shared_ptr<JdkCamera>;

#endif	// V4L2ISP_CAMERA_H
